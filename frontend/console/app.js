"use strict";

const SESSIONS_KEY = "bioagent_sessions_v1";
const UI_KEY = "bioagent_ui_v1";
const CONN_KEY = "bioagent_connection_v1";  // last live connection id — survives a reload so we can re-subscribe
const LASTCONN_KEY = "bioagent_lastconn_v1"; // summary of the last ready session — powers the "reattach" hint after a gateway restart
const RUNOWNER_KEY = "bioagent_runowner_v1"; // {connId, sids}: the chats whose run is in flight — survives a reload so results never leak to the active chat
const LASTRUN_KEY = "bioagent_lastrun_v1";   // last COMPLETED run_id — survives a refresh so we can still recognise this conn's own run
const STREAMS_KEY = "aiscientist_streams_v1"; // stream ids of answers this browser already saved — a reconnect replay of one is dropped
const PLANDRAFT_KEY = "aiscientist_plandraft_v1"; // {<chat id>: {run, base, text, open}}: each plan editor's unapplied text — survives a reload while that plan is still waiting
const COMPOSER_KEY = "aiscientist_composer_v1";   // {<chat id>: {draft, bound, roles}}: each chat's composer — survives a reload
// Every request runs the same way (RCHENLAB/AiScientist issue 21): the agent routes it (a literature
// search or an analysis: route "auto"), proposes a plan the user reviews before anything runs, no
// review gate is skipped, the PI chooses one scientist or a team and the pipeline, and what the run
// hands on is saved to the user's data. The server fixes the same values; they are sent too, so a
// page served beside an older server still gets a plan to review.
const FIXED_RUN_OPTIONS = Object.freeze({ plan_mode: true, autonomous: false, mode: "auto", save_results: true,
                                          presets: [], skills: [], preset_prompt: null, route: "auto" });

const state = {
  connectionId: null,
  ws: null,
  status: "disconnected",
  lastJobId: null,
  lastNode: null,
  lastGres: null,
  sessions: [],
  activeId: null,
  // Each chat's run and dialog live in its own record (see chatOf below), not here. What stays
  // here is the one visible chat's DOM: the live bubble of its run and its plan card.
  streamDom: null,      // {body, think, thinkBody, feed} DOM refs of the visible chat's live bubble
  maxBoundDatasets: null,
  selectedDatasetIds: new Set(),
  datasets: [],                // uploaded files/folders this session: {name, path, kind}, newest first
  serverRoots: [],             // server folders a dataset can be bound from by path (empty = off)
  serverOfferDismissed: new Set(),   // pasted server paths the user said "Not now" to
  uploads: [],                 // upload cards of the Datasets tab: {id,kind,name,size,sent,status,error,note,file,ctrl,jobId,serverFolder,chat,…}
  lastRunId: null,             // last completed run on this conn (used to recognise our own run after a refresh)
  leftAutoCollapsed: false,    // collapse the connect panel once, on first ready
  planCardEl: null,            // the visible chat's live plan / clarify / decision card
  planStatusEl: null,          // the resolved plan card's status line, updated as the PI works
  planAnsweredEl: null,        // the visible chat's entry for a plan it answered (drawAnsweredPlan)
  planView: null,              // a saved plan opened from the chat into the side panel ({agenda, doc, status})
  planStepOpen: new Map(),     // step text -> shown in full (true) or as two lines (false), as the person left it
  sideTab: "results",         // which of the side panel's two tabs is showing: "plan" | "results"
};

const $ = (id) => document.getElementById(id);

// ---- one dialog per chat ------------------------------------------------------------------------
// Each chat has its own dialog and its own run. The dialog: the data files attached for its next
// run, the case note, the draft in the composer, its context meter. The run: the live stream, the
// plan / clarify / decision card waiting for an answer, Stop and mid-run notes. None of it is shared
// between chats, so a chat opened while another one uploads or runs starts empty, and both can run
// at once — the server runs one run per chat, up to its per-session limit.
//
// `owned` = this window launched (or, after a reload, re-adopted) the chat's run. Only the owning
// window handles that run's events, so two windows open on one account never both save the answer.
const chats = new Map();   // session id -> chat record

function newChatRecord(sid) {
  return {
    sid,
    owned: false,          // this window handles this chat's run events
    running: false,        // the chat's run is in flight
    run: null,             // the live stream, DECOUPLED from the DOM so switching chats (or a reload)
                           // never detaches it: {text, thinking, feed:[], status, statusLabel,
                           // startedAt, runId, streamId}; `feed` is an ordered list of
                           // {kind:"line",text,level} milestones and {kind:"code",code} step code.
    suppress: false,       // the stream being replayed is one this browser already saved
    deadRunTimer: null,    // grace timer that stops a "running" chat whose backend run died silently
    planPending: false,    // a PI plan / clarify / decision is waiting for the user
    plan: null,            // that prompt: {kind: "plan"|"clarify"|"decision", sid, run, agenda, doc,
                           // revised, problems, questions, goal, options}. Its card is drawn
                           // FROM it (drawPlanPrompt), so it survives a rebuild of the chat stream.
    planDraftYaml: "",     // what is typed in the plan editor and not applied (or an edit just sent)
    planDraftBase: "",     // the plan document (its YAML) that text and the open editor belong to
    planEditorOpen: false, // the plan editor is open on the waiting card
    planWords: "",         // a change to the plan typed in the side panel's box and not sent yet
    planShown: "",        // which version of the waiting plan has opened the side panel already
    bindConfirm: null,     // a local Bind choice, kept apart from an agent request in this chat
    confirm: null,         // a confirmation the agent waits on (confirm_request): {run, kind, summary,
                           // detail, reversible, deadline, answer}. Its card is drawn FROM it
                           // (drawConfirmPrompt), like the plan's, and it is cleared by confirm_done.
    answered: null,        // the plan as it stood when it was answered and the PI took it back (a reply
                           // in words, an edit, a cancel, a review the server closed): {agenda, doc,
                           // revised, status}. Its entry and the side panel show it, read-only, until
                           // the next prompt or the chat's next run.
    bound: [],             // the BIND-SET (feature ②): the server paths of every data file attached
                           // for the next run (a VCF + a BED panel + a 2nd VCF). Ordered by attach;
                           // the server re-ranks to pick the primary. Posted as `datasets`.
    serverBoundTyped: new Set(), // paths already bound in this chat, as typed before resolving
    roles: {},             // path -> role the user set on a bound file ("reference" = comparison
                           // data such as a cell-type atlas; never picked as the primary).
    primary: null,         // the PRIMARY of the bind-set (highest-ranked), posted as `dataset_path`
    caseNote: null,        // a clinical description attached as TEXT: {name, text}. The composer no
                           // longer attaches one (issue 21: it goes in the message); nothing sets it.
    draft: "",             // what was typed in the composer and not sent yet
    ctx: null,             // the last context-occupancy report of this chat's fast-chat turn
  };
}

function chatOf(sid) {
  if (!sid) return null;
  sid = String(sid);
  let c = chats.get(sid);
  if (!c) { c = newChatRecord(sid); restoreComposer(c); chats.set(sid, c); }
  return c;
}
function activeChat() { return chatOf(state.activeId); }
function chatVisible(c) { return !!c && c.sid === String(state.activeId); }
function chatTitle(sid) {
  const s = state.sessions.find((x) => String(x.id) === String(sid));
  return (s && s.title) || "New chat";
}

// The dialog code and the plan card read these off `state`: they are the ACTIVE chat's fields.
function emptyChatField(field) {
  if (field === "serverBoundTyped") return new Set();
  return { bound: [], roles: {}, revised: [], problems: [], agenda: [] }[field] ??
    (field === "running" || field === "planPending" || field === "planEditorOpen" ? false
      : field === "planDraftYaml" || field === "planDraftBase" ? "" : null);
}
for (const [key, field] of Object.entries({ boundPaths: "bound", boundRoles: "roles", datasetPath: "primary",
  serverBoundTyped: "serverBoundTyped",
  caseNote: "caseNote", running: "running", planPending: "planPending", planPrompt: "plan",
  planDraftYaml: "planDraftYaml", planDraftBase: "planDraftBase", planEditorOpen: "planEditorOpen" })) {
  Object.defineProperty(state, key, {
    enumerable: true,
    get() { const c = activeChat(); return c ? c[field] : emptyChatField(field); },
    set(v) { const c = activeChat(); if (c) c[field] = v; },
  });
}
for (const [key, field] of Object.entries({ planDoc: "doc", planProblems: "problems",
  planRevisedSteps: "revised", planAgenda: "agenda" })) {
  Object.defineProperty(state, key, {
    enumerable: true,
    get() { const c = activeChat(); const v = c && c.plan ? c.plan[field] : undefined; return v === undefined ? emptyChatField(field) : v; },
    set(v) { const c = activeChat(); if (c) { c.plan = c.plan || {}; c.plan[field] = v; } },
  });
}

// ---- each chat's composer, kept across a reload -------------------------------------------------
// What was typed and not sent, the data attached for the next run and the run options belong to the
// chat they were set in (RCHENLAB/AiScientist issue 7). This browser keeps them per chat, so a reload
// gives every chat back its own and nothing of another's. The case note is not kept: it is a
// patient's clinical text, held only while the page is open. Logging out clears them all.
function readComposers() {
  let all = null;
  try { all = JSON.parse(localStorage.getItem(COMPOSER_KEY) || "null"); } catch {}
  return all && typeof all === "object" && !Array.isArray(all) ? all : {};
}
function writeComposers(all) {
  try {
    if (Object.keys(all).length) localStorage.setItem(COMPOSER_KEY, JSON.stringify(all));
    else localStorage.removeItem(COMPOSER_KEY);
  } catch { /* private mode or a full store: the chat keeps its composer until the page closes */ }
}
// Only what differs from a new chat is stored; a chat with nothing of its own has no entry.
function saveComposer(c) {
  if (!c) return;
  const entry = {};
  if (c.draft) entry.draft = c.draft;
  const serverBacked = state.sessions.some((s) => s.id === c.sid && s.cid);
  if (!serverBacked) {
    if (c.bound.length) entry.bound = [...c.bound];
    const roles = Object.entries(c.roles).filter(([p]) => c.bound.includes(p));
    if (roles.length) entry.roles = Object.fromEntries(roles);
  }
  const all = readComposers();
  if (Object.keys(entry).length) all[c.sid] = entry; else delete all[c.sid];
  writeComposers(all);
}
function restoreComposer(c) {
  const e = readComposers()[c.sid];
  if (!e || typeof e !== "object") return;
  if (typeof e.draft === "string") c.draft = e.draft;
  if (state.sessions.some((s) => s.id === c.sid && s.cid)) return;
  if (Array.isArray(e.bound)) c.bound = e.bound.filter((p) => typeof p === "string" && p);
  for (const [p, role] of Object.entries((e.roles && typeof e.roles === "object") ? e.roles : {})) {
    if (c.bound.includes(p) && typeof role === "string") c.roles[p] = role;
  }
}
// What an older page stored beside a chat's composer (its engine, Plan first, Bypass, Save results,
// mode) is no longer read, and the chat's next save drops it.
function forgetComposer(sid) {
  const all = readComposers();
  delete all[String(sid)];
  writeComposers(all);
}
// Entries of chats that no longer exist (deleted elsewhere, or another account's on this browser) go.
function pruneComposers(sessions) {
  const keep = new Set(sessions.map((s) => String(s.id)));
  const all = readComposers();
  for (const sid of Object.keys(all)) if (!keep.has(sid)) delete all[sid];
  writeComposers(all);
}

// The chats whose run is in flight, kept across a reload so their replayed results land in them.
function saveRunOwners() {
  const sids = [...chats.values()].filter((c) => c.owned && (c.running || c.planPending)).map((c) => c.sid);
  try {
    if (sids.length && state.connectionId) localStorage.setItem(RUNOWNER_KEY, JSON.stringify({ connId: state.connectionId, sids }));
    else localStorage.removeItem(RUNOWNER_KEY);
  } catch { /* private mode: a reload then loses only the live bubble */ }
}
function loadRunOwners(connId) {
  try {
    const ro = JSON.parse(localStorage.getItem(RUNOWNER_KEY) || "null");
    if (!ro || ro.connId !== connId) return [];
    return Array.isArray(ro.sids) ? ro.sids.map(String) : ro.sid ? [String(ro.sid)] : [];   // older pages stored one {sid}
  } catch { return []; }
}

// Stream ids of the answers this browser saved. A quick-chat answer has no run_id, so without them
// a reconnect replayed (and saved) the same finished answer once more each time.
function seenStream(id) {
  if (!id) return false;
  try { return (JSON.parse(localStorage.getItem(STREAMS_KEY) || "[]") || []).includes(id); } catch { return false; }
}
function rememberStream(id) {
  if (!id) return;
  try {
    const ids = (JSON.parse(localStorage.getItem(STREAMS_KEY) || "[]") || []).filter((x) => x !== id);
    ids.push(id);
    localStorage.setItem(STREAMS_KEY, JSON.stringify(ids.slice(-100)));
  } catch { /* private mode */ }
}

function toast(message) {
  const t = $("toast");
  t.textContent = message;
  t.classList.add("visible");
  setTimeout(() => t.classList.remove("visible"), 2800);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));
}

// Backend progress/feed lines carry a leading emoji as a semantic marker (🔬 scientist, ✅ step
// done, 🧭 coordinator, …). Render each as ONE unified single-color Google Material Symbol
// (fonts.google.com/icons — the same icon set used in buttons and summary rows) instead of the
// multicolor OS emoji. The emoji stays the wire format, so a missed mapping just falls back to the
// glyph. Runs on already-escaped HTML: the inserted spans are fixed strings, and the emoji chars
// are not HTML-significant, so this never introduces markup from user text.
const EMOJI_SYMBOL = {
  "📋": "assignment", "🗒": "assignment", "📝": "edit_note", "🔬": "science", "🧪": "science",
  "🔎": "search", "🔍": "search", "✅": "check_circle", "☑": "check_circle", "🧭": "explore",
  "🙋": "front_hand", "✋": "front_hand", "⚠": "warning", "❗": "priority_high", "✂": "content_cut",
  "✏": "edit", "📏": "straighten", "🗜": "compress", "📚": "menu_book", "🔀": "alt_route",
  "❓": "help", "📥": "download", "📤": "upload", "🔴": "error", "🟢": "check_circle", "⏳": "hourglass_empty",
  "💲": "payments",
};
// Match any mapped emoji, swallowing a trailing variation-selector (U+FE0F) so "⚠️" maps cleanly.
const EMOJI_RE = new RegExp("(" + Object.keys(EMOJI_SYMBOL).join("|") + ")️?", "g");
function withSymbols(html) {
  return html.replace(EMOJI_RE, (_m, e) =>
    `<span class="material-symbols-outlined msym feed-ico">${EMOJI_SYMBOL[e]}</span>`);
}

// GFM pipe tables: a header row, a |---|---| delimiter row, then body rows (leading/trailing pipes
// optional). Runs on ALREADY-escaped text — escapeHtml leaves "|" intact, so cells stay safe — and
// emits table HTML with no "*"/"_" of its own, so the **bold**/_italic_ passes that run afterwards
// still format cell contents without touching the tags. A model that prints a stats table now renders
// a real table instead of a wall of pipes.
function mdTables(s) {
  const lines = s.split("\n");
  const isDelim = (l) => l.includes("|") && l.includes("-") && /^[\s|:-]+$/.test(l);
  const cells = (l) => {
    let t = l.trim();
    if (t.startsWith("|")) t = t.slice(1);
    if (t.endsWith("|")) t = t.slice(0, -1);
    return t.split("|").map((c) => c.trim());
  };
  const out = [];
  for (let i = 0; i < lines.length; ) {
    const head = lines[i], delim = lines[i + 1];
    if (head && head.includes("|") && !isDelim(head) && delim && isDelim(delim)) {
      const cols = cells(head);
      const body = [];
      let j = i + 2;
      for (; j < lines.length && lines[j].includes("|") && lines[j].trim(); j++) body.push(cells(lines[j]));
      out.push(
        '<table class="md-table"><thead><tr>' + cols.map((c) => "<th>" + c + "</th>").join("") +
        "</tr></thead><tbody>" +
        body.map((r) => "<tr>" + cols.map((_c, k) => "<td>" + (r[k] || "") + "</td>").join("") + "</tr>").join("") +
        "</tbody></table>");
      i = j;
    } else { out.push(head); i++; }
  }
  return out.join("\n");
}

// ---- inline mermaid -------------------------------------------------------
// A ```mermaid fence renders as a real diagram in the chat, so the model can EXPLAIN a result
// with a picture instead of three paragraphs of prose. Constraints that shaped this:
//
//  * mmdc (mermaid-cli) is NOT installed on the prod server and prod has no guaranteed internet
//    egress, so this renders CLIENT-SIDE from a locally vendored mermaid.min.js — no CDN, no new
//    server dependency. (The backend's make_schematic tool is untouched: it renders graphviz
//    figure ARTIFACTS into a run bundle. Different job, different path.)
//  * The bundle is 2.7 MB, so it is fetched LAZILY — only once a message actually contains a
//    diagram. A user who never sees one never pays for it.
//  * The source is MODEL-GENERATED text. Two independent guarantees hold:
//      1. renderMarkdown escapes the source into a <pre> placeholder FIRST, so until mermaid
//         succeeds the page contains only escaped text — the pre-existing no-raw-markup
//         guarantee of renderMarkdown is not weakened at any point.
//      2. mermaid runs with securityLevel:"strict" (HTML labels off, scripts stripped) and we
//         only swap in its output AFTER a successful parse.
//    A diagram that fails to parse, or a mermaid bundle that fails to load, degrades to showing
//    the source — never to injected markup, and never to a silently blank message.
const MERMAID_SRC = "/static/mermaid.min.js";
let _mermaidLoad = null;      // Promise<mermaid|null>, resolved once per page

// Map the console's palette onto mermaid's "base" theme so a diagram reads as part of the page
// rather than as a pasted-in third-party image. Falls back to literals if a variable is missing.
function mermaidThemeVars() {
  const css = getComputedStyle(document.documentElement);
  const v = (name, fallback) => (css.getPropertyValue(name) || "").trim() || fallback;
  const text = v("--text", "#322e27");
  const border = v("--border-strong", "#d7ccb5");
  return {
    background: "transparent",
    primaryColor: v("--surface-sunken", "#f2ebdc"),
    primaryTextColor: text,
    primaryBorderColor: border,
    secondaryColor: v("--surface", "#fbf7f0"),
    tertiaryColor: v("--surface-hover", "#ece3d2"),
    lineColor: v("--text-tertiary", "#9c9382"),
    textColor: text,
    mainBkg: v("--surface-sunken", "#f2ebdc"),
    nodeBorder: border,
    clusterBkg: v("--surface", "#fbf7f0"),
    clusterBorder: v("--border", "#e5ddcc"),
    fontSize: "13px",
  };
}

function loadMermaid() {
  if (_mermaidLoad) return _mermaidLoad;
  _mermaidLoad = new Promise((resolve) => {
    if (window.mermaid) return resolve(window.mermaid);
    const el = document.createElement("script");
    el.src = MERMAID_SRC;
    el.onload = () => {
      if (!window.mermaid) return resolve(null);
      try {
        window.mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",   // no click handlers, no scripts, sanitized labels
          // htmlLabels:false is NOT redundant with securityLevel:"strict". Verified 2026-07-20 in
          // the harness below: with HTML labels ON, a node label of
          //   A["<img src=x onerror='...'>"]
          // renders as a <foreignObject> containing a REAL <img> element. strict mode does strip the
          // onerror (no script executes — confirmed), but the <img src> survives, so model text could
          // still emit a markup element that fires an outbound request. Turning HTML labels off makes
          // mermaid draw labels as SVG <text>, removing the surface rather than sanitizing it.
          htmlLabels: false,
          flowchart: { htmlLabels: false },
          // The console is a single warm ("tea") light palette — there is no dark mode — so a
          // hardcoded dark mermaid theme would drop a black box into a beige page. Use mermaid's
          // "base" theme driven by the console's OWN CSS variables, so diagrams inherit the palette
          // and keep matching it if the palette is ever retuned.
          theme: "base",
          themeVariables: mermaidThemeVars(),
          fontFamily: "inherit",
        });
      } catch { return resolve(null); }
      resolve(window.mermaid);
    };
    el.onerror = () => resolve(null);   // offline / asset missing → callers show the source
    document.head.appendChild(el);
  });
  return _mermaidLoad;
}

let _mermaidSeq = 0;

// Real Qwen output routinely writes multi-line labels the way a person would — a literal `\n`, or
// `<br>` / `<b>…</b>` inside a node label. With htmlLabels:false (a SECURITY choice, see below) the
// renderer draws labels as plain SVG <text>, so those constructs render as literal characters
// ("Do you need\nto…") or, when a `\n` lands next to a paren, break the parser and drop the whole
// diagram to its source. This normalises the SOURCE before parsing — it only ever REMOVES markup /
// flattens to plain text, so it cannot introduce an injection (the output sanitizer still runs), and
// the ORIGINAL text is what the fallback shows, so nothing is hidden if the diagram still fails.
function normalizeMermaidSource(src) {
  return String(src)
    .replace(/\\n/g, " ")                          // literal backslash-n the model puts in labels
    .replace(/<br\s*\/?>/gi, " ")                  // <br>, <br/>, <br /> → space
    .replace(/<\/?(?:b|i|u|em|strong)\b[^>]*>/gi, ""); // drop inline formatting tags in labels
}

// Defence in depth on mermaid's OUTPUT. htmlLabels:false + securityLevel:"strict" should already
// mean the SVG contains nothing but drawing primitives, but this is model-generated input rendered
// by a 2.7 MB third-party bundle, so the guarantee is enforced here rather than assumed: parse the
// SVG in an INERT document (DOMParser neither fetches nor executes), drop the embedding/scripting
// elements and every event-handler or javascript: attribute, and only then hand back markup.
// Returns null if the result isn't a usable <svg>, which callers treat like a parse failure.
const _MERMAID_BANNED = "script,img,image,iframe,object,embed,foreignObject,link,animate,set,use";

// <style> is deliberately NOT in that list. Mermaid emits its entire theme as an inline <style>
// inside the SVG, so stripping it does not harden anything — it just produces an unstyled diagram
// (verified: black boxes, labels detached from their shapes). Keep the element, remove only the two
// CSS constructs that can make an outbound request, which is the actual risk: a diagram can inject
// CSS via classDef / style directives or a %%{init}%% themeCSS block.
const _CSS_FETCH = /@import|url\s*\(/gi;

function sanitizeMermaidSvg(svg) {
  let doc;
  try {
    doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  } catch { return null; }
  const root = doc.documentElement;
  if (!root || root.tagName.toLowerCase() !== "svg" || doc.querySelector("parsererror")) return null;
  for (const bad of root.querySelectorAll(_MERMAID_BANNED)) bad.remove();
  for (const style of root.querySelectorAll("style")) {
    style.textContent = String(style.textContent || "").replace(_CSS_FETCH, "/*blocked*/");
  }
  for (const el of [root, ...root.querySelectorAll("*")]) {
    for (const attr of [...el.attributes]) {
      const name = attr.name.toLowerCase();
      const value = String(attr.value || "").replace(/\s/g, "").toLowerCase();
      if (name.startsWith("on") || value.startsWith("javascript:") || value.startsWith("data:text/html")) {
        el.removeAttribute(attr.name);
      }
    }
  }
  return root.outerHTML;
}

// Render every not-yet-rendered .md-mermaid placeholder inside `root`. Idempotent: a repaint
// mid-stream re-creates placeholders, and each is rendered at most once (the `done` flag).
// Streaming safety: a half-typed diagram is INVALID mermaid and would flash a parse error on
// every frame, so a placeholder is only rendered once its message is no longer streaming.
async function renderMermaidIn(root) {
  if (!root) return;
  const nodes = [...root.querySelectorAll(".md-mermaid:not([data-mermaid-done])")];
  if (!nodes.length) return;
  const mermaid = await loadMermaid();
  for (const node of nodes) {
    const src = node.textContent || "";
    // Parse/render a normalised copy (plain-text labels); the node keeps the ORIGINAL text, so a
    // fallback still shows exactly what the model wrote.
    const parseSrc = normalizeMermaidSource(src);
    node.setAttribute("data-mermaid-done", "1");
    if (!mermaid) {
      node.classList.add("md-mermaid-fallback");   // keep the escaped source visible
      continue;
    }
    const id = "mmd" + (++_mermaidSeq);
    try {
      // parse() first so a malformed diagram is rejected BEFORE anything is inserted; render()
      // on bad input can otherwise leave mermaid's own error graphic in the DOM.
      await mermaid.parse(parseSrc);
      const { svg } = await mermaid.render(id, parseSrc);
      const clean = sanitizeMermaidSvg(svg);
      if (!clean) throw new Error("diagram output failed sanitization");
      node.innerHTML = clean;
      node.classList.add("md-mermaid-ok");
    } catch (err) {
      // Degrade to the source, labelled. The model writes these; a broken one is a normal
      // outcome, not an error worth hiding the content over.
      node.classList.add("md-mermaid-fallback");
      node.setAttribute("title", "Diagram could not be rendered: " + (err && err.message ? err.message : err));
    }
  }
}

function renderMarkdown(text) {
  // Normalize PaperQA anchor-style citations before anything else: the chat has no #ref anchor
  // targets, so [3](#ref3) / [[1](#ref1), [2](#ref2)] render as raw noise. Collapse them to plain
  // [3] / [1, 2], and strip the literal <i>/<b> HTML the corpus citation titles carry. Display-
  // layer + deterministic, so it holds no matter what the model emits.
  text = String(text)
    .replace(/\[(\d+)\]\(#ref[^)]*\)/gi, "$1")   // [3](#ref3) -> 3  (outer [ ] stay -> [3])
    .replace(/\(#ref[^)]*\)/gi, "")                 // any stray (#refN) left behind
    .replace(/<\/?(?:i|b|sub|sup)>/gi, "");          // literal <i>/<b> tags in citation titles
  // Code first: stash fenced ``` blocks + inline `code` (already-escaped) so the inline rules below
  // never mangle their contents — this is what kept `target_sum` from turning into `target<em>sum</em>`
  // and made ```fenced``` blocks render as real code instead of literal backticks.
  const codes = [];
  const stash = (html) => "￼C" + (codes.push(html) - 1) + "￼";
  let s = String(text)
    // ```mermaid fences are stashed like any other code block — same escaping, same protection
    // from the inline rules — but tagged so renderMermaidIn() can upgrade them to SVG later.
    .replace(/```[ \t]*mermaid[ \t]*\n?([\s\S]*?)```/gi, (_m, body) =>
      stash('<pre class="md-mermaid">' + escapeHtml(body.replace(/\n+$/, "")) + "</pre>"))
    .replace(/```[ \t]*[\w-]*\n?([\s\S]*?)```/g, (_m, body) =>
      stash('<pre class="md-code"><code>' + escapeHtml(body.replace(/\n+$/, "")) + "</code></pre>"))
    .replace(/`([^`\n]+)`/g, (_m, c) => stash("<code>" + escapeHtml(c) + "</code>"));
  s = mdTables(escapeHtml(s))
    .replace(/^#### (.+)$/gm, "<h4>$1</h4>")
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h2>$1</h2>")
    .replace(/^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$/gm, "<hr>")
    // "> " lines (escaped to &gt; above) become one quote block
    .replace(/(?:^&gt; ?.*(?:\n|$))+/gm, (block) =>
      "<blockquote>" + block.replace(/\n$/, "").split("\n").map((l) => l.replace(/^&gt; ?/, "")).join("\n") + "</blockquote>")
    // group consecutive "- " / "* " lines into a real bullet list
    .replace(/(?:^[ \t]*[-*] .+(?:\n|$))+/gm, (block) =>
      "<ul>" + block.trim().split(/\n/).map((l) =>
        "<li>" + l.replace(/^[ \t]*[-*] /, "") + "</li>").join("") + "</ul>")
    // and consecutive "1. " / "1) " lines into a numbered one, keeping the first number
    .replace(/(?:^[ \t]*\d{1,3}[.)] .+(?:\n|$))+/gm, (block) => {
      const items = block.trim().split(/\n/);
      const first = parseInt(items[0], 10);
      return (first > 1 ? `<ol start="${first}">` : "<ol>") + items.map((l) =>
        "<li>" + l.replace(/^[ \t]*\d{1,3}[.)] /, "") + "</li>").join("") + "</ol>";
    })
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    // italic: underscores NOT flanked by word chars, so snake_case identifiers stay intact
    .replace(/(?<![A-Za-z0-9_])_([^_\n]+?)_(?![A-Za-z0-9_])/g, "<em>$1</em>");
  // The answer is shown with white-space: pre-wrap, so a newline next to a block (a heading, a
  // list, a table, a code block) would add an empty line on top of the block's own margin. Drop
  // those newlines and let the CSS margins space the blocks.
  return withSymbols(s).replace(/￼C(\d+)￼/g, (_m, i) => codes[+i])
    .replace(MD_BLOCK_OPEN, "$1").replace(MD_BLOCK_CLOSE, "$1");
}
const MD_BLOCK_OPEN = /\n+(<(?:h2|h3|h4|ul|ol|table|pre|blockquote|hr)[\s>])/g;
const MD_BLOCK_CLOSE = /(<\/(?:h2|h3|h4|ul|ol|table|pre|blockquote)>|<hr>)\n+/g;

function isUnreachable(err) {
  return err instanceof TypeError; // fetch rejects with TypeError when the server is unreachable
}

function rid() {
  return "s" + Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);
}

// Slurm "HH:MM:SS" (or "D-HH:MM:SS") -> seconds; 0 when absent/unparseable so callers treat the
// limit as unknown rather than as "already expired".
function hmsToSeconds(hms) {
  const s = String(hms || "").trim();
  if (!s) return 0;
  const [dPart, tPart] = s.includes("-") ? s.split("-") : ["0", s];
  const parts = tPart.split(":").map((n) => parseInt(n, 10));
  if (parts.some((n) => !Number.isFinite(n))) return 0;
  while (parts.length < 3) parts.unshift(0);      // "MM:SS" / "SS" -> pad to H:M:S
  const days = parseInt(dPart, 10) || 0;
  return days * 86400 + parts[0] * 3600 + parts[1] * 60 + parts[2];
}

// ---- sessions (server-backed when logged in; localStorage fallback) --------
// When accounts are ON (state.user set), chat history lives server-side via the
// /api/conversations + /api/conversations/<id>/messages endpoints, so it survives a
// browser/device change. When accounts are OFF (single-user/dev), we keep the old
// localStorage store. Each server call is best-effort: any failure degrades to the
// local behavior rather than breaking the UI.

function authed() { return !!state.user; }

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    throw new Error(data.detail || data.error || `${method} ${url} -> ${r.status}`);
  }
  return r.status === 204 ? null : r.json();
}

// Map a stored server message row -> the in-memory shape messageEl() renders.
function msgFromServer(m) {
  if (m.kind === "plan") return { role: m.role || "assistant", kind: "plan", content: m.content || "", plan: (m.meta || {}).plan || null };
  if (m.kind === "artifacts") {
    const meta = m.meta || {};
    return { role: m.role || "assistant", kind: "artifacts", items: meta.items || [], bundleUrl: meta.bundleUrl,
             streamId: meta.stream_id || null };
  }
  const msg = { role: m.role, content: m.content };
  // Restore the collapsed run recap (thinking + steps/code) so a finished run stays
  // reviewable after a reload / on another device — not only in a downloaded log.
  const meta = m.meta || {};
  if (meta.stream_id) msg.streamId = meta.stream_id;
  if (meta.thinking || (meta.feed && meta.feed.length)) {
    msg.thinking = meta.thinking || "";
    msg.feed = meta.feed || [];
  }
  return msg;
}

// The payload to persist a message server-side (artifacts: text empty, blobs by URL in meta).
function serverMsgPayload(msg) {
  // stream_id: the server keeps one copy of an answer that several windows save (see add_message).
  if (msg.kind === "plan") return { role: msg.role || "assistant", content: msg.content || "", kind: "plan", meta: { plan: msg.plan || null } };
  if (msg.kind === "artifacts") {
    return { role: msg.role || "assistant", content: "", kind: "artifacts",
             meta: { items: msg.items || [], bundleUrl: msg.bundleUrl, ...(msg.streamId ? { stream_id: msg.streamId } : {}) } };
  }
  const payload = { role: msg.role, content: msg.content || "" };
  if (msg.thinking || (msg.feed && msg.feed.length)) {
    payload.kind = "text";
    payload.meta = { thinking: msg.thinking || "", feed: msg.feed || [] };
  }
  if (msg.streamId) { payload.kind = "text"; payload.meta = { ...(payload.meta || {}), stream_id: msg.streamId }; }
  return payload;
}

function syncMessage(s, msg) {
  if (!authed() || !s || !s.cid) return;
  api("POST", `/api/conversations/${s.cid}/messages`, serverMsgPayload(msg)).catch(() => {});
}

// Lazily fetch a server conversation's messages the first time it's opened.
async function ensureMessages(s) {
  if (!s || s.loaded || !authed() || !s.cid) return;
  try {
    const data = await api("GET", `/api/conversations/${s.cid}`);
    s.messages = (data.messages || []).map(msgFromServer);
    s.title = data.conversation.title;
    hydrateChatDatasets(chatOf(s.id), data);
    if (s.id === state.activeId) syncDatasetSelection();
  } catch { /* keep whatever we have */ }
  s.loaded = true;
}

async function loadSessions() {
  if (authed()) {
    try {
      const data = await api("GET", "/api/conversations");
      state.sessions = (data.conversations || []).map((c) => ({
        id: String(c.id), cid: c.id, title: c.title, messages: [], loaded: false,
        createdAt: c.created_at, updatedAt: c.updated_at,
      }));
      pruneComposers(state.sessions);
      if (!state.sessions.length) { await newSession(); }
      else { state.activeId = state.sessions[0].id; showDialog(); await ensureMessages(activeSession()); }
      renderSessionList();
      renderChat();
      return;
    } catch {
      // Falling back is right; falling back SILENTLY is not. An empty localStorage renders exactly
      // like an account with no history — a fresh blank chat — so a failed fetch and "you have
      // never used this account" are indistinguishable on screen. That is the difference between a
      // transient glitch and believing your work is gone, so say which one it is.
      toast("Could not load your saved chats from the server — showing this device's local copy only.");
    }
  }
  try {
    const raw = JSON.parse(localStorage.getItem(SESSIONS_KEY) || "[]");
    state.sessions = Array.isArray(raw) ? raw : [];
  } catch { state.sessions = []; }
  if (!state.sessions.length) newSession();
  else { state.activeId = state.sessions[0].id; showDialog(); }
}

function persistSessions() {
  if (authed()) return;   // server-backed: every change is synced via its own endpoint
  try { localStorage.setItem(SESSIONS_KEY, JSON.stringify(state.sessions.slice(0, 50))); } catch {}
}

function activeSession() {
  return state.sessions.find((s) => s.id === state.activeId) || null;
}

async function newSession() {
  leaveDialog();
  if (authed()) {
    try {
      const c = (await api("POST", "/api/conversations", {})).conversation;
      state.sessions.unshift({ id: String(c.id), cid: c.id, title: c.title, messages: [], loaded: true,
      createdAt: c.created_at, updatedAt: c.updated_at || c.created_at });
      state.activeId = String(c.id);
      state.planView = null;
      renderSessionList();
      renderChat();
      showDialog();
      return;
    } catch { /* fall through to a local-only session */ }
  }
  const s = { id: rid(), title: "New chat", messages: [], loaded: true, createdAt: Date.now() };
  state.sessions.unshift(s);
  state.activeId = s.id;
  state.planView = null;
  persistSessions();
  renderSessionList();
  renderChat();
  showDialog();
}

async function selectSession(id) {
  leaveDialog();
  state.activeId = id;
  state.planView = null;
  renderSessionList();
  showDialog();
  await ensureMessages(activeSession());
  renderChat();
}

// Keep what was typed in the chat being left; show the dialog of the chat being entered — its
// attachments, case note, draft, uploads and context meter, never the previous chat's.
function leaveDialog() {
  const c = activeChat(), input = $("chatInput");
  if (c && input) { c.draft = input.value; saveComposer(c); }
}
function showDialog() {
  const c = activeChat(), input = $("chatInput");
  if (input) input.value = (c && c.draft) || "";
  syncDatasetSelection();
  renderUploadLine();
  // Occupancy is per conversation and only known once THIS chat takes a turn — the previous
  // conversation's number would be worse than showing nothing.
  if (c && c.ctx) renderContextMeter(c.ctx); else hideContextMeter();
  updateComposerButton();
}

// ---- pipeline and skill text, as the System page shows it ----------------------------------------
const METHOD_DATA_TYPES = { scrna: "scRNA-seq", variants: "Variants (VCF)" };

// A protocol file is hard-wrapped at about 90 columns. Shown as it is, in a box of another width,
// every line breaks twice. So the lines of one paragraph or list item are joined, and only the
// breaks between blocks (blank lines, list items, headings, table rows, code) are kept.
function methodUnwrap(text) {
  const out = [];
  let fence = false;
  const block = /^\s*(?:[-*+] |\d+[.)] |#{1,6} |\||>)/;
  for (const line of String(text == null ? "" : text).split("\n")) {
    if (/^\s*```/.test(line)) { fence = !fence; out.push(line); continue; }
    const prev = out[out.length - 1];
    const fresh = fence || !line.trim() || block.test(line) || prev === undefined || !prev.trim()
      || /^\s*(?:#{1,6} |\||```)/.test(prev);
    if (fresh) out.push(line);
    else out[out.length - 1] = prev.replace(/\s+$/, "") + " " + line.trim();
  }
  return out.join("\n");
}

// The composer's placeholder, unless a plan is waiting in this chat (then it asks what to change).
function syncPlaceholder() {
  const input = $("chatInput");
  if (input && !state.planPending) input.placeholder = DEFAULT_PLACEHOLDER;
}

// Pull the (owner, run_id) of every finished run a chat owns, from the artifact
// URLs we stored (/api/bundle/<owner>/<run_id> or /api/artifacts/<owner>/<run_id>/…).
function runRefsOfSession(s) {
  const refs = [];
  const seen = new Set();
  for (const m of (s && s.messages) || []) {
    if (m.kind !== "artifacts") continue;
    const url = m.bundleUrl || (m.items && m.items[0] && m.items[0].url) || "";
    const mt = url.match(/\/api\/(?:bundle|artifacts)\/([^/]+)\/([^/]+)/);
    if (!mt) continue;
    const key = mt[1] + "/" + mt[2];
    if (seen.has(key)) continue;
    seen.add(key);
    refs.push({ owner: mt[1], run_id: mt[2] });
  }
  return refs;
}

// True if we've ALREADY got this finished run (so a reconnect replay of it is a
// duplicate we should ignore). LASTRUN_KEY covers the common case even when the
// owner chat's messages aren't loaded into memory; the per-session scan catches
// the rest. A run we've never seen (missed its live completion) returns false, so
// the replay is allowed to recover it.
function alreadyHaveRun(runId) {
  if (!runId) return false;
  try { if (localStorage.getItem(LASTRUN_KEY) === runId) return true; } catch {}
  for (const s of state.sessions) {
    for (const r of runRefsOfSession(s)) if (r.run_id === runId) return true;
  }
  return false;
}

async function deleteSession(id) {
  // If this chat has a run in flight, stop it first so the GPU/compute it's holding is
  // released — a run left orphaned by a deleted chat keeps consuming.
  const chat = chats.get(String(id));
  if (chat && (chat.running || chat.planPending)) {
    stopRun(chat);
    setChatRunning(chat, false);
  }
  // A prompt that was waiting in this chat has nowhere left to be answered.
  if (chat) { if (chatVisible(chat)) removePlanCard(); releasePlanPrompt(chat); dropPlanDraft(chat); }
  const gone = state.sessions.find((s) => s.id === id);
  // Load this chat's messages first (server-backed chats load lazily) so we know
  // which run artifacts to clean up even if it was never opened this session.
  await ensureMessages(gone);
  // Delete this chat's generated artifacts on the server too, so the downloadable
  // notebook/bundle don't outlive the chat. Fire-and-forget — disk cleanup must
  // not block (or fail) the UI removal.
  for (const ref of runRefsOfSession(gone)) {
    fetch("/api/results/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(ref),
    }).catch(() => {});
  }
  // Remove the conversation record server-side (cascades its messages).
  if (authed() && gone && gone.cid) api("DELETE", `/api/conversations/${gone.cid}`).catch(() => {});
  state.sessions = state.sessions.filter((s) => s.id !== id);
  chats.delete(String(id));
  forgetComposer(id);
  saveRunOwners();
  if (state.activeId === id) {
    if (state.sessions.length) { state.activeId = state.sessions[0].id; showDialog(); await ensureMessages(activeSession()); }
    else { await newSession(); return; }
  }
  persistSessions();
  renderSessionList();
  renderChat();
}

// A chat's age, in the sidebar. Shown relative because that is how someone looks for a chat
// ("the one from yesterday"); the exact stamp is the tooltip.
function absTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleString();
}

function relTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const mins = Math.floor((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

// What the sidebar says about a chat whose prompt is waiting: the card is only on screen while
// that chat is, so this is how a person in another chat learns that something waits for them.
const PLAN_WAITING_LABEL = { plan: "Plan to review", clarify: "Question for you", decision: "Decision needed" };

function renderSessionList() {
  const list = $("sessionList");
  if (!state.sessions.length) { list.innerHTML = '<div class="session-empty">No chats yet.</div>'; return; }
  list.innerHTML = state.sessions
    .map((s) => `
      <div class="session-item ${s.id === state.activeId ? "active" : ""}" data-id="${s.id}">
        <span class="session-text">
          <span class="session-title">${escapeHtml(s.title || "New chat")}</span>
          <span class="session-meta">${sessionBadge(s.id)}<span class="session-time" title="${escapeHtml(absTime(s.updatedAt || s.createdAt))}">${escapeHtml(relTime(s.updatedAt || s.createdAt))}</span></span>
        </span>
        <button class="session-del" data-del="${s.id}" title="Delete chat" aria-label="Delete chat">✕</button>
      </div>`)
    .join("");
}

// A chat that waits for an answer says what it waits for; a chat whose run is in flight says so.
// Several chats may be either at once.
function sessionBadge(sid) {
  const c = chats.get(String(sid));
  if (!c) return "";
  if (c.plan) return `<span class="session-waiting" title="This chat is waiting for your answer">${PLAN_WAITING_LABEL[c.plan.kind] || "Waiting for you"}</span>`;
  if (c.confirm && !c.confirm.answer) return '<span class="session-waiting" title="The agent is waiting for your OK in this chat">Needs your OK</span>';
  if (c.bindConfirm) return '<span class="session-waiting" title="A Bind of server data is waiting for your OK in this chat">Needs your OK</span>';
  if (c.running) return '<span class="session-running" title="A run is in progress in this chat">Running</span>';
  return "";
}

// ---- chat rendering -------------------------------------------------------

function renderChat() {
  const s = activeSession();
  const c = activeChat();
  $("chatTitle").textContent = s ? s.title || "New chat" : "New chat";
  renderChatUsage();
  if (c && !c.usage) refreshChatUsage(c);
  const stream = $("chatStream");
  stream.innerHTML = "";
  state.streamDom = null;
  // The plan card went with the rest of the stream. It is not a saved message: a prompt still
  // waiting in this chat is drawn again below, from the chat's record.
  state.planCardEl = null;
  state.planStatusEl = null;
  state.planAnsweredEl = null;
  // Always re-derive the downloads panel from the ACTIVE session — including the
  // empty-session case below, which used to early-return and leave the previous
  // session's downloads stranded in the panel.
  refreshDownloads();
  syncPlaceholder();
  if (!s || (!s.messages.length && !ownerVisible() && !planPromptVisible() && !confirmPromptVisible())) {
    stream.innerHTML = '<div class="empty-hint">Connect to HPC3, then ask a research question.</div>';
    renderPlanPanel();
    updateComposerButton();
    return;
  }
  for (const m of s.messages) stream.appendChild(messageEl(m));
  // If this chat has a run in flight, (re)mount its live bubble and repaint it from its record —
  // so switching back to a running chat resumes the live stream instead of showing a
  // frozen/blank bubble — and bring back the card it is waiting on.
  mountStream();
  // Same for a plan, a question or a decision point that is waiting in this chat: its card with
  // its buttons comes back, instead of a composer that says a plan is pending and nothing to act on.
  drawPlanPrompt();
  // …and a confirmation the agent is waiting on in this chat.
  drawConfirmPrompt();
  // …and the side panel's Plan tab follows the chat on screen: its waiting plan, else its last one.
  renderPlanPanel();
  if (c && c.plan && c.plan.kind === "plan") selectSideTab("plan");
  // The composer follows the chat on screen: its Send/Stop button and its placeholder are this
  // chat's, and both were last set for the chat that was left.
  updateComposerButton();
  stream.scrollTop = stream.scrollHeight;
}

// ---- confirmation cards ---------------------------------------------------
// Before the agent acts at an edge of what it may do on HPC3 (install a package into the lab's
// shared area, write outside the user's folders, download, delete or overwrite), the server asks:
// `confirm_request`, answered with POST /api/confirm, closed by `confirm_done`. Like a plan, the
// request is held in its own chat's record (c.confirm) and its card is drawn from there, so it comes
// back when that chat is shown again and is never drawn into another chat; after a reconnect or a
// reload the server sends it again. No answer before its deadline counts as no on the server.
const CONFIRM_TITLES = {
  install: "📦 Install software on HPC3?",
  download: "⬇️ Download onto HPC3?",
  write_outside: "✍️ Write outside your folders?",
  destructive: "⚠️ Delete or overwrite on HPC3?",
  quota: "💾 Use more storage on HPC3?",
  budget: "💳 Over your API budget — continue?",
};
// A budget question stops the run on no, unlike the agent's other requests (key_guard, issue 16).
const BUDGET_OUTCOMES = {
  approved: "✓ Continuing past your limit for this run.",
  declined: "✗ Declined — the run stopped before its next model call.",
  timeout: "No answer in time — counted as no; the run stopped before its next model call.",
};
const CONFIRM_OUTCOMES = {
  approved: "✓ Approved — the agent continues.",
  declined: "✗ Declined — the agent continues without it.",
  timeout: "No answer in time — counted as no; the agent continues without it.",
  stopped: "The run stopped — nothing was done.",
};

function confirmPromptVisible(c = activeChat()) {
  return !!c && !!(c.confirm || c.bindConfirm) && chatVisible(c);
}

// When the request counts as no: the time left (a request sent again after a reconnect) or its
// whole window (a new one), from now. null when the server gave neither.
function confirmDeadline(msg) {
  const raw = msg.seconds_left ?? msg.timeout_s;
  if (raw == null) return null;
  const left = Number(raw);
  return Number.isFinite(left) ? Date.now() + 1000 * left : null;
}

function holdConfirm(c, msg) {
  const next = { run: msg.run_id || null, kind: String(msg.kind || ""), summary: String(msg.summary || ""),
    detail: String(msg.detail || ""), reversible: msg.reversible !== false, deadline: confirmDeadline(msg), answer: null };
  const held = c.confirm;
  const same = !!held && held.run === next.run && held.kind === next.kind && held.summary === next.summary
    && held.detail === next.detail;
  // The same request sent again (a reconnect) after it was answered here: the answer stands.
  if (same && held.answer) return;
  c.confirm = next;
  renderSessionList();
  if (chatVisible(c)) {
    if (same && state.confirmCardEl && state.confirmCardEl.isConnected) { state.confirmCardEl.innerHTML = confirmCardHtml(next); wireConfirmCard(c); return; }
    drawConfirmPrompt(c);
    return;
  }
  if (!same) toast(`“${chatTitle(c.sid).trim()}” needs your OK — open that chat to answer.`);
}

function confirmCardHtml(p, note = "") {
  const when = p.deadline ? new Date(p.deadline).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  const meta = [p.kind === "budget" ? "No stops the run; its files are kept." : p.reversible ? "This can be undone." : "This cannot be undone.",
    when ? `No answer by ${when} counts as no.` : "No answer in time counts as no."].join(" ");
  const foot = note
    ? `<div class="confirm-status">${escapeHtml(note)}</div>`
    : p.answer
      ? `<div class="confirm-status">${p.answer === "approve" ? "Approving…" : "Declining…"}</div>`
      : `<div class="plan-actions-row"><button type="button" class="primary" data-confirm="approve">${escapeHtml(p.approveLabel || "Approve")}</button>` +
        `<button type="button" class="ghost" data-confirm="decline">${escapeHtml(p.declineLabel || "Decline")}</button></div>`;
  return `<h2>${escapeHtml(p.title || CONFIRM_TITLES[p.kind] || "🔐 The agent needs your OK")}</h2>` +
    `<div class="clarify-question">${escapeHtml(p.summary)}</div>` +
    (p.detail ? `<pre class="confirm-detail">${escapeHtml(p.detail)}</pre>` : "") +
    (note || p.kind === "bind" ? "" : `<div class="confirm-meta">${escapeHtml(meta)}</div>`) + foot;
}

function removeConfirmCard() {
  if (state.confirmCardEl) { state.confirmCardEl.remove(); state.confirmCardEl = null; }
}

function wireConfirmCard(c, el = state.confirmCardEl, onAnswer = (approved) => answerConfirm(c, approved)) {
  if (!el) return;
  el.querySelectorAll("[data-confirm]").forEach((b) =>
    b.addEventListener("click", () => onAnswer(b.dataset.confirm === "approve")));
}

// Both agent requests and local Bind requests use the same card and button wiring.
function mountConfirmCard(p, onAnswer) {
  const stream = $("chatStream");
  const hint = stream.querySelector(".empty-hint"); if (hint) hint.remove();
  const el = document.createElement("div");
  el.className = "msg assistant plan-card confirm-card";
  el.dataset.prompt = "confirm";
  el.innerHTML = confirmCardHtml(p);
  stream.appendChild(el);
  wireConfirmCard(null, el, onAnswer);
  stream.scrollTop = stream.scrollHeight;
  return el;
}

function drawConfirmPrompt(c = activeChat()) {
  if (!c || !chatVisible(c)) return;
  if (state.bindConfirmCardEl) { state.bindConfirmCardEl.remove(); state.bindConfirmCardEl = null; }
  if (c.bindConfirm) {
    state.bindConfirmCardEl = mountConfirmCard(c.bindConfirm, c.bindConfirm.resolve);
  }
  if (!c.confirm) return;
  removeConfirmCard();
  state.confirmCardEl = mountConfirmCard(c.confirm, (approved) => answerConfirm(c, approved));
}

function confirmServerBind(c, message) {
  if (!c || c.bindConfirm) return Promise.resolve(false);
  return new Promise((resolve) => {
    const p = { kind: "bind", title: "Bind server data?", detail: message,
      summary: "Bind this copy as data for this run?", approveLabel: "Bind", declineLabel: "Cancel",
      resolve: (approved) => {
        if (c.bindConfirm !== p) return;
        c.bindConfirm = null;
        renderSessionList();
        if (chatVisible(c) && state.bindConfirmCardEl) {
          state.bindConfirmCardEl.remove(); state.bindConfirmCardEl = null;
        }
        resolve(approved);
      } };
    c.bindConfirm = p;
    // The chat list marks the chat as waiting, like an agent request (sessionBadge).
    renderSessionList();
    if (chatVisible(c)) drawConfirmPrompt(c);
    else toast(`“${chatTitle(c.sid).trim()}” needs your OK to bind server data — open that chat to answer.`);
  });
}

// One large-copy Bind waits for an answer at a time in a chat.
function bindWaiting(c) {
  return !!(c && c.bindConfirm);
}

// The request is no longer waiting: its card keeps a line saying what became of it.
function closeConfirm(c, note) {
  const p = c.confirm;
  if (!p) return;
  c.confirm = null;
  renderSessionList();
  if (chatVisible(c) && state.confirmCardEl && state.confirmCardEl.isConnected) {
    state.confirmCardEl.innerHTML = confirmCardHtml(p, note);
    state.confirmCardEl = null;
  }
}

function releaseConfirm(c, msg) {
  if (!c.confirm) return;
  const outcomes = c.confirm.kind === "budget" ? { ...CONFIRM_OUTCOMES, ...BUDGET_OUTCOMES } : CONFIRM_OUTCOMES;
  closeConfirm(c, outcomes[msg && msg.outcome] || "This request is no longer waiting.");
}

async function answerConfirm(c, approved) {
  const p = c.confirm;
  if (!p || p.answer || !state.connectionId) return;
  p.answer = approved ? "approve" : "decline";
  if (chatVisible(c) && state.confirmCardEl) state.confirmCardEl.innerHTML = confirmCardHtml(p);
  renderSessionList();
  let body = {};
  try {
    const r = await fetch("/api/confirm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, approved, run_id: p.run, conversation_id: c.sid }),
    });
    body = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  } catch (err) {
    // Not sent: the buttons come back, so the person can try again before the deadline.
    if (c.confirm === p) {
      p.answer = null;
      if (chatVisible(c) && state.confirmCardEl) { state.confirmCardEl.innerHTML = confirmCardHtml(p); wireConfirmCard(c); }
      renderSessionList();
    }
    toast("Could not send your answer: " + err.message);
    return;
  }
  // The server took it (confirm_done follows and closes the card) or no longer waits for it.
  if (c.confirm === p && body.status === "stale") closeConfirm(c, "This request is no longer waiting.");
  else if (c.confirm === p) closeConfirm(c, approved ? CONFIRM_OUTCOMES.approved : CONFIRM_OUTCOMES.declined);
}

function refreshDownloads() {
  const s = activeSession();
  const last = s && [...s.messages].reverse().find((m) => m.kind === "artifacts");
  if (last) renderDownloads(last.items, last.bundleUrl);
  else $("downloads").innerHTML = '<div class="downloads-empty">Run a pipeline to get a results bundle (.zip) and browse the report, figures, and tables here.</div>';
}

function messageEl(m) {
  if (m.kind === "plan" && m.plan) {
    // An approved plan: its entry, which opens the whole plan in the side panel.
    const el = document.createElement("div");
    el.className = "msg assistant plan-card plan-sheet plan-entry saved";
    el.innerHTML = planEntryHtml(m.plan.agenda || [], m.plan.doc || null, { status: m.plan.status || "Approved" });
    el.querySelector('[data-plan="open"]').addEventListener("click", () => { state.planView = m.plan; openPlanPanel(); renderPlanPanel(); });
    return el;
  }
  if (m.kind === "artifacts") {
    const el = document.createElement("div");
    el.className = "artifacts-block";
    const bundle = m.bundleUrl ? `<a class="artifact-bundle" href="${m.bundleUrl}" download>${icon("download")}Download all results (.zip)</a>` : "";
    const n = (m.items && m.items.length) || 0;
    el.innerHTML = `<div class="artifacts-title">${icon("folder_open")}<span>Results &amp; code (${n})</span></div>` + bundle +
      `<div class="result-handoff"></div>` +
      `<div class="artifacts-hint">Browse &amp; preview every file — organized by folder — in the Downloads panel →</div>`;
    const run = (m.bundleUrl || "").match(/\/api\/bundle\/[^/]+\/([^/]+)/);
    if (run) showResultHandoff(el.querySelector(".result-handoff"), run[1]);
    el.addEventListener("click", onSaveResultClick);
    return el;
  }
  const el = document.createElement("div");
  el.className = `msg ${m.role}`;
  if (m.role !== "assistant") { el.innerHTML = escapeHtml(m.content); return el; }
  // Assistant: an optional COLLAPSED recap of the run (thinking + steps/code) stays with
  // the message for later review — default-collapsed so it doesn't clutter, expand to audit.
  let html = "";
  if (m.thinking) {
    html += `<details class="think recap">${summaryRow("psychology", "Thinking & activity")}` +
      `<div class="think-body">${escapeHtml(m.thinking)}</div></details>`;
  }
  if (m.feed && m.feed.length) {
    const milestones = m.feed.filter((e) => e.kind !== "code").length;
    html += `<details class="run-recap">${summaryRow("science", `Steps & code (${milestones})`)}` +
      `<div class="lab-progress done">${m.feed.map(feedItemHtml).join("")}</div></details>`;
  }
  html += `<div class="msg-body">${renderMarkdown(m.content || "")}</div>`;
  el.innerHTML = html;
  // Upgrade any ```mermaid placeholder to a real diagram. Async + fire-and-forget: the element is
  // returned (and appended) synchronously, so a slow/absent mermaid bundle can never delay or block
  // painting the message — the diagram just resolves into place a moment later, or stays as source.
  // Deliberately NOT called from repaintStream(): mid-stream a diagram is half-typed and therefore
  // invalid, and re-parsing it every frame would flash errors. The live bubble is replaced by this
  // persisted message at chat_done (finishAssistantStream -> renderChat), which is where it lands.
  renderMermaidIn(el);
  return el;
}

// HTML for one persisted feed entry — a key-progress line or a collapsed step-code block.
function feedItemHtml(entry) {
  if (entry.kind === "code") {
    return `<details class="step-code">${summaryRow("data_object", "Step code")}` +
      `<pre class="step-code-body"><code>${escapeHtml(entry.code || "")}</code></pre></details>`;
  }
  const raw = entry.text || "";
  const sub = /^\s{2,}/.test(raw);
  return `<div class="progress-line ${entry.level || "info"}${sub ? " sub" : ""}">${withSymbols(escapeHtml(raw.trim()))}</div>`;
}

// The dataset the chat is bound to, as a short stem for the title: "Ddx41_DEG1", or "Ddx41_DEG1 +2"
// when several files are attached. "" when nothing is bound yet.
function datasetLabel() {
  const primary = primaryBoundPath();
  if (!primary) return "";
  const d = state.datasets.find((x) => x.path === primary);
  const name = (d && d.name) || primary.split("/").pop() || "";
  const stem = name.replace(/\.(gz|bgz|zip)$/i, "").replace(/\.[A-Za-z0-9]{1,6}$/, "").slice(0, 22);
  const extra = state.boundPaths.length - 1;
  return stem + (extra > 0 ? ` +${extra}` : "");
}

// A chat's title has to say WHICH DATASET as well as what was asked. Titled on the prompt alone,
// 46 of the 60 conversations in production all read "What changes between DDX41 mutant and WT r"
// and were impossible to tell apart in the sidebar. Dataset + question separates different studies;
// re-running the SAME question on the SAME data is the common case and separates on nothing, so a
// title that would collide with one already in the list is stamped with the time — the only thing
// that actually differs between those runs.
function sessionTitle(prompt) {
  const gist = String(prompt || "").replace(/\s+/g, " ").trim().slice(0, 42);
  const ds = datasetLabel();
  const base = (ds ? `${ds} · ${gist}` : gist) || "New chat";
  if (!state.sessions.some((x) => x.id !== state.activeId && (x.title || "") === base)) return base;
  const t = new Date();
  const p2 = (n) => String(n).padStart(2, "0");
  return `${base} · ${p2(t.getMonth() + 1)}-${p2(t.getDate())} ${p2(t.getHours())}:${p2(t.getMinutes())}`;
}

function pushMessage(msg) {
  const s = activeSession();
  if (!s) return;
  s.messages.push(msg);
  if (msg.role === "user" && (s.title === "New chat" || !s.title)) {
    s.title = sessionTitle(msg.content);
    $("chatTitle").textContent = s.title;
    renderSessionList();
    if (authed() && s.cid) api("PATCH", `/api/conversations/${s.cid}`, { title: s.title }).catch(() => {});
  }
  syncMessage(s, msg);
  persistSessions();
}

// Push to a SPECIFIC session by id (the run owner), not whatever is active now —
// keeps streamed results in the chat that started them.
function pushToSession(sessionId, msg) {
  const s = state.sessions.find((x) => x.id === sessionId);
  if (!s) return;
  // A replayed answer this chat already shows (saved here, or loaded from the server) is not added again.
  if (msg.streamId && s.messages.some((m) => m.streamId === msg.streamId && (m.kind || "") === (msg.kind || ""))) return;
  s.messages.push(msg);
  syncMessage(s, msg);
  persistSessions();
}

function appendUserMessage(text) {
  const stream = $("chatStream");
  const hint = stream.querySelector(".empty-hint");
  if (hint) hint.remove();
  pushMessage({ role: "user", content: text });
  stream.appendChild(messageEl({ role: "user", content: text }));
  state.stickBottom = true;   // the user just sent — snap to their message and follow the reply
  stream.scrollTop = stream.scrollHeight;
}

// Each chat's run stream lives in its record. Its live bubble is painted into the DOM only while
// that chat is the visible one — so switching to another chat can never see (or steal) another
// chat's run, and switching BACK re-mounts and repaints from the record.
function ownerVisible() { const c = activeChat(); return !!(c && c.run); }

let runTicker = null;
function startAssistantStream(c) {
  // A plan, clarify or decision prompt clears startedAt (holdPlanPrompt); approving it starts it again.
  c.run = { text: "", thinking: "", feed: [], status: "running", statusLabel: "", startedAt: Date.now(), runId: null, streamId: null };
  if (chatVisible(c)) { state.streamDom = null; mountStream(); }
  // Heartbeat: keep the "Working…" timer ticking even when no events arrive for a while (long
  // HPC3 compute steps are silent), so a run never looks frozen. One ticker serves every chat.
  if (!runTicker) {
    runTicker = setInterval(() => {
      if (ownerVisible()) repaintWorking();
      if (![...chats.values()].some((x) => x.run)) { clearInterval(runTicker); runTicker = null; }
    }, 1000);
  }
}

// Human elapsed like "8s", "1m 20s", "12m 03s".
function fmtElapsed(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), r = s % 60;
  return `${m}m ${String(r).padStart(2, "0")}s`;
}

// Ensure the live bubble exists in the CURRENT chat DOM (only when the owner chat is
// visible) and repaint it from state. Safe to call repeatedly / after a re-render.
function mountStream() {
  if (!ownerVisible()) { state.streamDom = null; return; }
  const stream = $("chatStream");
  const hint = stream.querySelector(".empty-hint");
  if (hint) hint.remove();
  let wrap = stream.querySelector(".msg.assistant.streaming");
  if (wrap && stream.contains(wrap) && stream.lastElementChild !== wrap) {
    // Keep the live bubble as the LAST child. In plan mode the bubble is created at
    // chat_start (before the plan card is appended below it); once the run resumes we
    // move it back to the bottom so live activity appears AFTER the approved plan, not
    // stranded above it.
    stream.appendChild(wrap);
  }
  if (!wrap || !stream.contains(wrap)) {
    wrap = document.createElement("div");
    wrap.className = "msg assistant streaming";
    const think = document.createElement("details");
    think.className = "think";
    think.open = true;
    think.style.display = "none";
    think.innerHTML = summaryRow("psychology", "activity") + '<div class="think-body"></div>';
    const feed = document.createElement("div");
    feed.className = "lab-progress";
    feed.style.display = "none";
    const working = document.createElement("div");
    working.className = "stream-working";
    working.innerHTML = '<span class="working-dot"></span><span class="working-text"></span>';
    const body = document.createElement("div");
    body.className = "msg-body";
    wrap.appendChild(think);
    wrap.appendChild(feed);
    wrap.appendChild(working);
    wrap.appendChild(body);
    stream.appendChild(wrap);
  }
  state.streamDom = {
    wrap,
    think: wrap.querySelector(".think"),
    thinkBody: wrap.querySelector(".think-body"),
    feed: wrap.querySelector(".lab-progress"),
    working: wrap.querySelector(".stream-working"),
    workingText: wrap.querySelector(".working-text"),
    body: wrap.querySelector(".msg-body"),
  };
  repaintStream();
}

// Render one feed entry ({kind:"line",...} milestone or {kind:"code",...} step code) into
// the always-visible key-progress area.
function feedEntryEl(entry) {
  if (entry.kind === "code") {
    const d = document.createElement("details");
    d.className = "step-code";
    d.innerHTML = summaryRow("data_object", "Step code") + `<pre class="step-code-body"><code></code></pre>`;
    d.querySelector("code").textContent = entry.code || "";
    return d;
  }
  const raw = entry.text || "";
  const sub = /^\s{2,}/.test(raw);
  const line = document.createElement("div");
  line.className = `progress-line ${entry.level || "info"}${sub ? " sub" : ""}`;
  line.innerHTML = withSymbols(escapeHtml(raw.trim()));
  return line;
}

// --- smooth streaming: coalesce repaints to one per frame + sticky (non-yanking) scroll --------
// The old path re-rendered the whole bubble on EVERY token (jank) and force-scrolled to the bottom
// each time (yanking the reader down even when they'd scrolled up). Now: tokens accumulate in state
// and we repaint at most once per animation frame; scroll only follows when the user is already at
// the bottom. Reading history mid-stream no longer drags you away.
let _repaintQueued = false;
function scheduleRepaint() {
  if (_repaintQueued) return;
  _repaintQueued = true;
  requestAnimationFrame(() => { _repaintQueued = false; repaintStream(); });
}
function nearBottom(el, pad = 90) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= pad;
}
// Follow new content only when "stuck" to the bottom (default true until the user scrolls up).
function stickToBottom() {
  const s = $("chatStream");
  if (s && state.stickBottom !== false) s.scrollTop = s.scrollHeight;
}

// Repaint the visible chat's live bubble from its run (idempotent full render — cheap; feeds are short).
function repaintStream() {
  const dom = state.streamDom;
  const c = activeChat();
  const run = c && c.run;
  if (!dom || !run) return;
  if (run.thinking) { dom.think.style.display = ""; dom.thinkBody.textContent = run.thinking; }
  else dom.think.style.display = "none";
  if (run.feed.length) {
    dom.feed.style.display = "";
    dom.feed.innerHTML = "";
    for (const entry of run.feed) dom.feed.appendChild(feedEntryEl(entry));
  } else dom.feed.style.display = "none";
  dom.body.innerHTML = run.text ? renderMarkdown(run.text) : "";
  repaintWorking();
  stickToBottom();
}

// Live "Working…" liveness line — pulsing dot + latest activity + elapsed timer.
// Kept ticking by the 1s heartbeat so a silent compute step never looks frozen.
function repaintWorking() {
  const dom = state.streamDom, c = activeChat(), run = c && c.run;
  if (!dom || !dom.working || !run) return;
  if (run.status !== "running" || c.planPending) { dom.working.style.display = "none"; return; }
  const secs = Math.floor((Date.now() - (run.startedAt || Date.now())) / 1000);
  // Prefer the REAL backend activity (model_call / tool_start) over a generic label.
  const label = run.text ? "Writing…" : (run.statusLabel || "Working…");
  // After a long silent stretch, reassure that compute is running (not stuck).
  const hint = secs >= 30 && !run.text ? " · this can take a few minutes" : "";
  dom.working.style.display = "flex";
  dom.workingText.textContent = run.startedAt == null ? label : `${label} · ${fmtElapsed(secs * 1000)}${hint}`;
}

// ---- stream event ingest (DOM-independent; always updates the chat's record) -----------
function streamToken(c, token) { if (c.run) { if (!c.planPending && c.run.startedAt == null) c.run.startedAt = Date.now(); c.run.text += token; if (chatVisible(c)) scheduleRepaint(); } }
function streamThinking(c, token) { if (c.run) { c.run.thinking += token; if (chatVisible(c)) scheduleRepaint(); } }
function streamProgress(c, msg) { if (c.run) { c.run.feed.push({ kind: "line", text: msg.text || "", level: msg.level || "info" }); if (chatVisible(c)) scheduleRepaint(); } }
// Transient "what's happening right now" label from the backend (model_call / tool_start).
// Drives the live working line so it shows real activity, not a generic "Working…".
function streamStatus(c, text) { if (c.run) { if (!c.planPending && c.run.startedAt == null) c.run.startedAt = Date.now(); c.run.statusLabel = text || ""; if (chatVisible(c)) repaintWorking(); } }
function streamCode(c, code) {
  if (!c.run) return;
  // The last successful run_code of a step wins: drop the previous trailing code block.
  const f = c.run.feed;
  if (f.length && f[f.length - 1].kind === "code") f.pop();
  f.push({ kind: "code", code: code || "" });
  if (chatVisible(c)) scheduleRepaint();
}

// ---- chat context occupancy -----------------------------------------------
// How full the fast-chat prompt is, reported once per chat turn. It exists because the old
// behaviour (silently keep the last 12 messages) gave the user NO way to tell that the
// assistant had stopped remembering the start of the conversation.
//
// Defensive on purpose: the Research route and any older/legacy backend send no chat_context
// event at all, so the chip simply never appears — it must not render an empty or "NaN" box.
function fmtTokens(n) {
  if (!(n >= 1000)) return String(n);
  if (n >= 1e6) { const m = n / 1e6; return (m >= 10 ? Math.round(m) : Math.round(m * 10) / 10) + "M"; }
  const k = n / 1000;
  return (k >= 10 ? Math.round(k) : Math.round(k * 10) / 10) + "K";
}

function renderContextMeter(msg) {
  const el = $("ctxMeter");
  if (!el) return;
  const used = Number(msg && msg.used);
  const allowed = Number(msg && msg.allowed);
  if (!isFinite(used) || !isFinite(allowed) || allowed <= 0) { el.classList.add("hidden"); return; }
  const pct = Math.max(0, Math.min(100, Math.round((used / allowed) * 100)));
  el.classList.remove("hidden");
  el.classList.toggle("compacted", !!msg.compacted);
  el.classList.toggle("warn", !msg.compacted && pct >= 80);
  el.innerHTML = `<span class="ctx-bar"><i class="ctx-fill" style="width:${pct}%"></i></span>` +
    `<span>${fmtTokens(used)} / ${fmtTokens(allowed)}</span>`;
  const how = msg.exact ? "counted by the model's own tokenizer" : "estimated from characters";
  el.title = `Chat context: ${used.toLocaleString()} of ${allowed.toLocaleString()} prompt tokens (${pct}%, ${how}).` +
    (msg.compacted
      ? "\n\nThis conversation is over its budget, so older turns have been folded into a running summary. The recent exchanges are still sent word-for-word."
      : "\n\nThe whole conversation still fits — nothing has been summarized or dropped.");
}

function hideContextMeter() { const el = $("ctxMeter"); if (el) el.classList.add("hidden"); }

function finishAssistantStream(c) {
  finishAssistantTurn(c, false);
}

// awaitingPlan: the turn ended because its plan was saved and is waiting (a saved plan holds no
// run): its card, and any draft typed into its editor, stay.
function finishAssistantTurn(c, awaitingPlan) {
  // The run is over, so nothing of it can still be waiting for an answer. A run that ended while its
  // card waited may send no plan_done: without this its card would be drawn again on every switch of chats.
  if (!awaitingPlan && (c.plan || c.planPending)) { if (chatVisible(c)) removePlanCard(); hidePlanPanel(c); }
  if (c.confirm) closeConfirm(c, "The run ended — this request is no longer waiting.");
  if (!awaitingPlan) dropPlanDraft(c);
  if (!c.run) { saveRunOwners(); return; }
  const run = c.run;
  // Persist the finished report to the chat that ran it (which may no longer be active), and
  // KEEP the run recap with it (thinking + steps/code) — collapsed, for later review —
  // instead of discarding it when the live bubble is replaced.
  if (run.text || run.thinking || run.feed.length) {
    pushToSession(c.sid, {
      role: "assistant",
      content: run.text || "",
      thinking: run.thinking || "",
      feed: run.feed.slice(),
      streamId: run.streamId || null,
    });
  }
  rememberStream(run.streamId);
  const wasVisible = chatVisible(c);
  c.run = null;
  saveRunOwners();
  // If that chat is on screen, rebuild it from persisted messages so the live
  // (ephemeral) bubble is replaced by the saved final message cleanly.
  if (wasVisible) { state.streamDom = null; renderChat(); }
}

function appendArtifacts(c, items, bundleUrl) {
  if (!items || !items.length) return;
  // Artifacts arrive after chat_done, when the live stream is gone: they carry its id from chat_start.
  const msg = { role: "assistant", kind: "artifacts", items, bundleUrl, streamId: c.lastStreamId || null };
  resultHandoffs = null;                // this run may have handed something on: ask again
  pushToSession(c.sid, msg);
  // Only touch the visible chat + downloads panel if the run's own chat is the one
  // on screen — otherwise these would bleed into whatever chat the user switched to.
  if (chatVisible(c)) {
    $("chatStream").appendChild(messageEl(msg));
    stickToBottom();
    renderDownloads(items, bundleUrl);
    if (!c.plan) selectSideTab("results");
  }
}

// Results panel: a small zip button + a two-tab area (Files = folder tree, Preview =
// inline view of the selected code/text file — Claude-style). No thumbnail wall.
function renderDownloads(items, bundleUrl) {
  const d = $("downloads");
  const zip = bundleUrl
    ? `<a class="dl-zip" href="${bundleUrl}" download title="Download the full results bundle (.zip)">${icon("download")}<span>zip</span></a>`
    : "";
  // These two are conversational entry points, NOT mechanical actions: they focus the composer and
  // hint what to say. The PI's follow-up router (_dispatch_lab) then decides edit-report vs re-run-step
  // — inferring WHICH step from your words — and executes. No manual step-picking in the UI.
  const rerun = `<button type="button" class="dl-regen" data-rerun title="Ask the PI to re-run an analysis step. Click, then say what to change in the chat — e.g. 'redo clustering at resolution 1.0'; the PI picks the right step, re-runs it (and everything after), and updates the report.">${icon("replay")}<span>Re-run a step</span></button>`;
  d.innerHTML =
    `<div class="results-bar">${zip}${rerun}<div class="results-tabs">` +
      `<button type="button" class="rtab-btn active" data-rtab="files">${icon("folder_open")}<span>Files</span></button>` +
      `<button type="button" class="rtab-btn" data-rtab="preview">${icon("visibility")}<span>Preview</span></button>` +
    `</div></div>` +
    `<div class="rtab-pane active" data-pane="files"><div id="fileTree" class="file-tree"><div class="preview-empty">Loading…</div></div></div>` +
    `<div class="rtab-pane" data-pane="preview"><div id="filePreviewPane" class="file-preview-pane">` +
      `<div class="preview-empty">Select a code / text file under Files to preview it here.</div></div></div>`;
  const m = (bundleUrl || (items && items[0] && items[0].url) || "").match(/\/api\/(?:bundle|file|artifacts)\/([^/]+)\/([^/]+)/);
  if (m) {
    loadResults(m[1], m[2]);
    // This run's id — the target of "Re-run a step". Persisted so a refresh keeps it working.
    state.lastRunId = m[2];
    try { localStorage.setItem(LASTRUN_KEY, m[2]); } catch {}
  }
}

// The results-panel "Re-run a step" button is a conversational entry point, not a mechanical
// action. Clicking it focuses the composer and hints what to say; the message the user then sends
// is routed by the PI's follow-up router (gateway `_dispatch_lab`), which infers WHICH step from
// the wording, re-runs it and updates the report. So the user never hand-picks a step number.
function primeComposer(kind) {
  if (!state.connectionId) { toast("Connect to a session first"); return; }
  if (state.running) { toast("A task is running — let it finish first"); return; }
  const input = $("chatInput");
  if (!input) return;
  input.placeholder = "Tell the PI what to change — e.g. “redo clustering at resolution 1.0”. It'll re-run the right step and update the report.";
  toast("Say what to change — the PI re-runs the right step.");
  input.focus();
  const stream = $("chatStream");
  if (stream) stream.scrollTop = stream.scrollHeight;
}

function switchResultsTab(name) {
  document.querySelectorAll("#downloads .rtab-btn").forEach((b) => b.classList.toggle("active", b.dataset.rtab === name));
  document.querySelectorAll("#downloads .rtab-pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === name));
}

// Code/text files preview INLINE in the Preview tab; pdf/image (+ anything else) keep the
// existing modal (heavier viewers we deliberately don't reimplement here).
const TEXT_PREVIEW_EXT = new Set([
  "py", "json", "md", "markdown", "txt", "text", "log", "csv", "tsv", "yaml", "yml",
  "toml", "ini", "cfg", "conf", "r", "sh", "js", "css", "html", "xml", "tex", "bib", "dot",
]);
function isTextPreview(name) { return TEXT_PREVIEW_EXT.has((name.split(".").pop() || "").toLowerCase()); }

function openResultFile(url, kind, name) {
  if (isTextPreview(name)) loadInlinePreview(url, name);
  else openFilePreview(url, kind, name);   // pdf / image → existing modal
}

async function loadInlinePreview(url, name) {
  const pane = $("filePreviewPane");
  if (!pane) return;
  switchResultsTab("preview");
  pane.innerHTML = '<div class="preview-empty">Loading…</div>';
  try {
    const t = await (await fetch(url)).text();
    pane.innerHTML =
      `<div class="preview-file-head"><span class="preview-file-name">${escapeHtml(name)}</span>` +
      `<a class="ghost small" href="${url}" download title="Download">${icon("download")}</a></div>` +
      `<pre class="preview-code"><code></code></pre>`;
    pane.querySelector("code").textContent = t.slice(0, 200000);   // textContent → no XSS
  } catch (e) { pane.innerHTML = `<div class="preview-empty">Preview failed: ${escapeHtml(e.message)}</div>`; }
}

// Material Symbols (fonts.google.com/icons) — one consistent icon set across the app,
// replacing the ad-hoc emoji. `icon()` returns the ligature span; `fileIcon()` maps a
// file kind to its symbol.
function icon(name) { return `<span class="material-symbols-outlined msym">${name}</span>`; }
// A <details> summary row: a leading Material Symbol + label + a chevron that rotates on open.
// One consistent icon row app-wide, replacing the old bare emoji glyphs in thinking/steps/code.
function summaryRow(name, label) {
  return `<summary>${icon(name)}<span class="sum-label">${escapeHtml(label)}</span>` +
    `<span class="material-symbols-outlined msym chev">expand_more</span></summary>`;
}
function fileIcon(kind) {
  return icon(({ table: "table_chart", markdown: "description", pdf: "picture_as_pdf",
                 text: "description", image: "image" })[kind] || "draft"); }

function fmtSize(n) {
  if (!n && n !== 0) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB";
  if (n < 1024 ** 3) return (n / 1024 / 1024).toFixed(1) + " MB";
  if (n < 1024 ** 4) return (n / 1024 ** 3).toFixed(2) + " GB";
  return (n / 1024 ** 4).toFixed(2) + " TB";
}

// Human label + sort order for the bundle's top-level folders.
const FOLDER_META = {
  report: { label: icon("description") + " Report", order: 0 },
  figures: { label: icon("image") + " Figures", order: 1 },
  tables: { label: icon("table_chart") + " Tables", order: 2 },
  data: { label: icon("genetics") + " Data", order: 3 },
  process: { label: icon("settings") + " Process", order: 4 },
  extra: { label: icon("folder") + " Extra", order: 9 },
  "": { label: icon("folder_open") + " Files", order: 5 },
};

// A collapsible folder TREE grouped by the bundle's top-level folder — every file (incl.
// images) is a row you click to preview. Because results are already produced in a folder
// layout (report/figures/tables/…), this maps cleanly onto an explorer.
async function loadResults(owner, runId) {
  const area = $("fileTree");
  if (!area) return;
  try {
    const data = await (await fetch(`/api/files/${owner}/${runId}`)).json();
    const files = data.files || [];
    if (!files.length) { area.innerHTML = '<div class="preview-empty">No files.</div>'; return; }

    const groups = {};
    for (const f of files) {
      const top = f.path.includes("/") ? f.path.split("/")[0] : "";
      (groups[top] ||= []).push(f);
    }
    const meta = (k) => FOLDER_META[k] || { label: icon("folder") + " " + escapeHtml(k), order: 6 };
    area.innerHTML = Object.keys(groups)
      .sort((a, b) => (meta(a).order - meta(b).order) || a.localeCompare(b))
      .map((k) => {
        const rows = groups[k]
          .sort((a, b) => a.name.localeCompare(b.name))
          .map((f) =>
            `<button class="file-row" data-url="${f.url}" data-kind="${f.kind}" data-name="${escapeHtml(f.name)}" title="${escapeHtml(f.path)}">` +
            `<span class="file-row-icon">${fileIcon(f.kind)}</span>` +
            `<span class="file-row-text"><span class="file-row-name">${escapeHtml(f.name)}</span>${declaredCaption(f.declared)}</span>` +
            `<span class="file-row-size">${fmtSize(f.size)}</span></button>`
          ).join("");
        return `<details class="file-folder" open><summary>${meta(k).label} <span class="file-folder-count">${groups[k].length}</span></summary>${rows}</details>`;
      }).join("");
  } catch { /* preview is best-effort */ }
}

// The description a skill or pipeline declared for a produced file, and the step that made it
// (process/declared_outputs.json, via /api/files). Nothing for a file no declaration matched.
function declaredCaption(d) {
  if (!d || !d.description) return "";
  const bad = { empty: "empty", blank: "blank image", invalid: "unreadable" }[d.status];
  return `<span class="file-row-desc">`
    + (d.step ? `<span class="file-row-step">Step ${escapeHtml(String(d.step))}</span>` : "")
    + escapeHtml(d.description)
    + (bad ? ` <span class="file-row-bad">${icon("warning")}${escapeHtml(bad)}</span>` : "")
    + `</span>`;
}

async function openFilePreview(url, kind, name) {
  const modal = $("filePreviewModal"), body = $("filePreviewBody");
  $("filePreviewTitle").textContent = name;
  $("filePreviewDownload").href = url;
  body.innerHTML = "Loading…";
  modal.classList.remove("hidden");
  try {
    if (kind === "image") body.innerHTML = `<img class="preview-img" src="${url}" alt="${escapeHtml(name)}" />`;
    else if (kind === "pdf") body.innerHTML = `<iframe class="preview-frame" src="${url}"></iframe>`;
    else { const t = await (await fetch(url)).text(); body.innerHTML = `<pre class="preview-text">${escapeHtml(t.slice(0, 200000))}</pre>`; }
  } catch (e) { body.textContent = "Preview failed: " + e.message; }
}

// ---- log ------------------------------------------------------------------

function appendLog(event) {
  // The event/error log panel was removed from the researcher UI (it duplicated the
  // chat). The full feed is persisted into each run's bundle (process/event_log.txt) by
  // the server. If the panel isn't present, this is a no-op — events still drive the
  // provisioning pipeline via updatePipelineFromEvent().
  const stream = $("logStream");
  if (!stream) return;
  const entry = document.createElement("div");
  entry.className = `log-entry ${event.level || "info"}`;
  const time = (event.created_at || "").slice(11, 19);
  let html = `<div class="meta">${escapeHtml(time)} · ${escapeHtml(event.stage || "")}</div><div>${escapeHtml(event.message || "")}</div>`;
  if (event.detail) {
    html += `<span class="detail-toggle">Show full cause ▾</span><pre class="hidden">${escapeHtml(formatDetail(event.detail))}</pre>`;
  }
  entry.innerHTML = html;
  const toggle = entry.querySelector(".detail-toggle");
  if (toggle) toggle.addEventListener("click", () => {
    const pre = entry.querySelector("pre");
    pre.classList.toggle("hidden");
    toggle.textContent = pre.classList.contains("hidden") ? "Show full cause ▾" : "Hide full cause ▴";
  });
  stream.appendChild(entry);
  stream.scrollTop = stream.scrollHeight;
}

function formatDetail(detail) {
  if (typeof detail === "string") return detail;
  const parts = [];
  if (detail.type) parts.push(`type: ${detail.type}`);
  if (detail.message) parts.push(`message: ${detail.message}`);
  if (detail.stage) parts.push(`stage: ${detail.stage}`);
  if (detail.command_failure) {
    const cf = detail.command_failure;
    parts.push(`\n$ ${cf.command}`, `exit status: ${cf.exit_status}`);
    if (cf.stdout) parts.push(`stdout:\n${cf.stdout}`);
    if (cf.stderr) parts.push(`stderr:\n${cf.stderr}`);
    if (cf.hint) parts.push(`hint: ${cf.hint}`);
  }
  if (detail.cause) parts.push(`\ncause: ${detail.cause.type}: ${detail.cause.message}`);
  if (detail.traceback) parts.push(`\n${detail.traceback}`);
  return parts.length ? parts.join("\n") : JSON.stringify(detail, null, 2);
}

// ---- pipeline -------------------------------------------------------------

const STAGE_TO_STEP = {
  ssh_connect: "ssh", ssh_auth: "ssh",
  ollama_detect: "ollama", ollama_install: "ollama",
  gpu_alloc: "gpu", ssh_tunnel: "serve", ollama_serve: "serve",
  ollama_model: "model", ready: "ready",
};
const STEP_ORDER = ["ssh", "ollama", "gpu", "serve", "model", "ready"];

function setStep(stepKey, cls) {
  const idx = STEP_ORDER.indexOf(stepKey);
  if (idx < 0) return;
  if (cls === "done" || cls === "active") {
    for (let i = 0; i < idx; i++) {
      const prev = document.querySelector(`#pipeline li[data-stage="${STEP_ORDER[i]}"]`);
      if (prev && !prev.classList.contains("failed")) { prev.classList.remove("active"); prev.classList.add("done"); }
    }
  }
  const li = document.querySelector(`#pipeline li[data-stage="${stepKey}"]`);
  if (li) { li.classList.remove("active", "done", "failed"); li.classList.add(cls); }
}

function resetPipeline() {
  document.querySelectorAll("#pipeline li").forEach((li) => li.classList.remove("active", "done", "failed"));
}

function updatePipelineFromEvent(event) {
  const step = STAGE_TO_STEP[event.stage];
  if (!step) return;
  if (event.level === "error") setStep(step, "failed");
  else if (event.level === "success") setStep(step, "done");
  else setStep(step, "active");
}

// ---- connection status / left-panel state ---------------------------------

// The server's runs in flight. A newer gateway lists every one (`live_runs`); an older one names
// only the newest (`active_run` + `chat_running`).
function liveRunsOf(summary) {
  if (Array.isArray(summary.live_runs)) return summary.live_runs;
  return summary.chat_running && summary.active_run ? [{ ...summary.active_run, running: true }] : [];
}

// Reload/reconnect restore of each chat's run, and silent-death reconciliation; the server is the
// source of truth for which chats have a run in flight. A listed run is re-adopted only by a chat
// this window owns (its owners survive a reload in storage) or by the chat on screen — so Stop
// targets it — never by a chat another window started. A chat that still shows a run the server
// no longer lists either gets its terminal event replayed (which clears it) or its run DIED with no
// terminal event (backend restarted, job reclaimed): after a grace period and a fresh look at the
// server, say so instead of spinning forever.
function reconcileRuns(summary) {
  if (summary.max_runs) state.maxRuns = summary.max_runs;
  const live = liveRunsOf(summary);
  const listed = new Set(live.map((r) => String(r.conversation_id || "")));
  for (const r of live) {
    if (!r.conversation_id) continue;
    const sid = String(r.conversation_id);
    const known = chats.get(sid);
    if (known && known.deadRunTimer) { clearTimeout(known.deadRunTimer); known.deadRunTimer = null; }
    if (r.running === false || !((known && known.owned) || sid === String(state.activeId))) continue;
    const c = chatOf(sid);
    if (!c.owned) c.owned = true;
    if (!c.running) setChatRunning(c, true);
  }
  for (const c of chats.values()) {
    if (!c.running || listed.has(c.sid) || c.deadRunTimer) continue;
    c.deadRunTimer = setTimeout(async () => {
      c.deadRunTimer = null;
      if (!c.running || !state.connectionId) return;
      let again = null;
      try {
        const res = await fetch(`/api/connections/${state.connectionId}`);
        if (res.ok) again = await res.json();
      } catch { return; }   // cannot look: leave the chat as it is
      if (!again || !c.running || liveRunsOf(again).some((r) => String(r.conversation_id) === c.sid)) return;
      finishAssistantStream(c);
      setChatRunning(c, false);
      toast(`The run in “${chatTitle(c.sid)}” ended with no result — the backend restarted or the job was reclaimed.`);
    }, 2500);
  }
}

// The header's second line: the model this session reasons with and where it runs (RCHENLAB/AiScientist
// issue 32). Empty without a session, where the status on the right already says Disconnected.
function sessionModelLine(summary) {
  if (!summary || summary.status === "disconnected" || summary.status === "error") return "";
  const ep = summary.llm_endpoint || {};
  const name = (m) => String(m || "").replace(/\/+$/, "").split("/").pop();
  let line;
  if (ep.kind === "shared") line = `${name(ep.model) || "Shared model"} · lab's shared server`;
  else if (ep.kind === "credential") line = `${name(ep.model) || ep.label || "API model"} · your API key`;
  else if (ep.kind === "cluster") line = `${ep.cluster_label || name(summary.selected_model || ep.model) || "Cluster model"} · your GPU on HPC3`;
  else if (ep.kind === "missing") line = ep.label || "";
  else line = name(summary.selected_model || summary.model);
  return line && summary.mock ? `${line} · mock` : line;
}

function applyStatus(summary) {
  const prevStatus = state.status;
  state.status = summary.status;
  $("headerModel").textContent = sessionModelLine(summary);
  const dot = $("connDot");
  dot.className = "dot " + (summary.status === "ready" ? "ready" : summary.status === "error" ? "error" : summary.status === "disconnected" ? "muted" : "connecting");
  const labels = { connecting: "Connecting…", provisioning: "Provisioning…", ready: `Live · ${summary.model}`, error: "Error", disconnected: "Disconnected" };
  $("connLabel").textContent = labels[summary.status] || summary.status;
  $("chatModel").textContent = summary.status === "ready" && summary.model ? `· ${summary.model}` : "";

  const isLogin = summary.status === "disconnected" || summary.status === "error";
  // ONE lifecycle: connecting → provisioning (SSH + GPU + model, in one shot) → ready. The
  // session is only usable at "ready" — there is no half-connected state to special-case.
  const isReady = summary.status === "ready";
  if (isReady) state.everConnected = true;
  if (isReady) restoreUploads();   // copies still running, and uploads this browser had not finished
  const isProvisioning = summary.status === "connecting" || summary.status === "provisioning";
  // Full provisioning pipeline view while the session is coming up for the first time; once it
  // has been live, keep the chat on screen and report progress inline instead.
  const showProvisionView = isProvisioning && !state.everConnected;
  const showConnected = isReady || (isProvisioning && state.everConnected);
  // only a lost/failed connection kills a run — every chat's
  if (isLogin) for (const c of chats.values()) if (c.running) setChatRunning(c, false);
  // Without a connection a prompt cannot be answered (submitPlan has nowhere to send it), so it is
  // not kept to be drawn again. A session that is still alive replays it when the socket reopens.
  if (summary.status === "disconnected") {
    for (const c of chats.values()) if (c.plan) { if (chatVisible(c)) removePlanCard(); hidePlanPanel(c); }
  }
  if (showConnected) reconcileRuns(summary);
  // Panel ergonomics: once live, collapse the connect panel to give the chat room
  // (once only — don't fight the user if they reopen it); reopen it when disconnected
  // so the connect form is reachable again.
  // Once live, collapse BOTH side panels (once) to maximize the chat — the user can
  // reopen either. Reopen the left on disconnect so the connect form is reachable.
  if (showConnected && !state.leftAutoCollapsed) { setPanel("left", false); setPanel("right", false); state.leftAutoCollapsed = true; }
  if (isLogin) { setPanel("left", true); state.leftAutoCollapsed = false; state.everConnected = false; }
  $("loginSection").classList.toggle("hidden", !isLogin);
  $("provisionSection").classList.toggle("hidden", !showProvisionView);
  $("connectedSection").classList.toggle("hidden", !showConnected);
  if (isReady) setStep("ready", "done");

  if (showConnected) {
    // Remember whose HPC3 account this session logged in with. With accounts disabled that
    // UCInetID IS the credential owner, and the login form's input is empty after a reload —
    // so without this the key dialog would look in the anonymous "guest" store and report the
    // user's own saved endpoints as missing.
    state.hpcUser = summary.username || state.hpcUser || "";
    $("connUser").textContent = `${summary.username || "you"} @ ${(summary.host || "").split(".")[0]}`;
    // The non-ready branch is only reachable after the session HAS been live (showConnected keeps
    // the chat up while it comes back), so say what's happening rather than naming a model.
    $("connSub").textContent = isReady
      ? `${summary.selected_model || summary.model}${summary.mock ? " · mock" : ""}`
      : "Bringing the GPU session up…";
  }
  if (isReady) {
    // Which LLM answers, and — when that is off-site — a banner that stays up for as long as it
    // is selected. Loaded once per session; after that the status stream keeps the picker honest.
    if (!apiKeys.creds.length && !state._apiKeysLoaded) { state._apiKeysLoaded = true; loadApiCredentials(); }
    renderEndpointSelect(summary);
    renderEgressBanner(summary);
    // A reusable SSH key was just minted+deployed — tell the user and refresh the picker.
    const nc = summary.new_credential;
    if (nc && nc.id && state._credToastedId !== nc.id) {
      state._credToastedId = nc.id;
      toast("Saved an SSH key on HPC3 — next login, pick 'Saved SSH key' to skip password + Duo.");
      loadCredentials();
    }
    // Remember enough to offer a "reattach" hint if the gateway later restarts: the GPU
    // job lives on HPC3 (squeue + the on-cluster port file), so a fresh login reuses it.
    if (!summary.mock) {
      try {
        localStorage.setItem(LASTCONN_KEY, JSON.stringify({
          account: state.user ? state.user.username : null,
          username: summary.username || "", host: summary.host || "",
          model: summary.selected_model || summary.model || "",
          jobId: (summary.gpu && summary.gpu.job_id) || null,
          node: (summary.gpu && summary.gpu.node) || null, ts: Date.now(),
          // Slurm kills the job at its --time wall limit, so the reattach hint can age itself out
          // instead of claiming a long-dead job is "still running".
          limitSec: hmsToSeconds(summary.gpu && summary.gpu.time_limit),
        }));
      } catch {}
    }
  }
  if (summary.gpu) renderGpu(summary);
  if (showConnected && summary.session) renderSessionState(summary.session);
  else if (isLogin) renderSessionState(null);
  if (isLogin || isReady) hideDuoPanel();

  // Cold-start status card in the chat area (replaces the removed log panel as a liveness
  // cue). Shown while provisioning; a brief "Live" tick on first-ready, then it closes.
  if (isProvisioning) {
    clearTimeout(_bootHideTimer);
    if ($("bootStatus").classList.contains("hidden")) showBoot("Starting your GPU session…", { sub: BOOT_SUB });
  } else if (isReady) {
    if (prevStatus !== "ready") {
      showBoot(BOOT_STAGE_LABEL.ready, { level: "success", done: true });
      clearTimeout(_bootHideTimer);
      _bootHideTimer = setTimeout(hideBoot, 1800);
    }
  } else {
    hideBoot();
  }
}

// ---- cold-start status card (lightweight; drives off the technical event feed) --------
const BOOT_STAGE_LABEL = {
  ssh_connect: "Connecting to HPC3…",
  ssh_auth: "Authenticating (SSH / Duo)…",
  ollama_detect: "Checking the model server…",
  ollama_install: "Preparing the model server…",
  gpu_alloc: "Waiting for a GPU (Slurm queue)…",
  ssh_tunnel: "Opening the secure tunnel…",
  ollama_serve: "Starting the model server…",
  llm_serve: "Loading the model into the GPU…",
  ollama_model: "Loading the model…",
  llm_model: "Loading the model…",
  ready: "Live — you can start chatting.",
};
const BOOT_SUB = "A GPU is being allocated and the model loaded — this can take a few minutes.";
let _bootHideTimer = null;

function showBoot(text, opts = {}) {
  const el = $("bootStatus"); if (!el) return;
  el.classList.remove("hidden");
  el.className = `boot-status ${opts.level || ""}`;
  const ico = opts.done ? icon("check_circle")
    : opts.level === "error" ? icon("error")
    : '<span class="boot-spinner"></span>';
  const sub = opts.sub ? `<div class="boot-sub">${escapeHtml(opts.sub)}</div>` : "";
  el.innerHTML = `<div class="boot-row">${ico}<span class="boot-text">${escapeHtml(text)}</span></div>${sub}`;
}

function hideBoot() { const el = $("bootStatus"); if (el) { el.classList.add("hidden"); el.innerHTML = ""; } }

// One event → update the card's current line. Errors stick (red); 'ready' is handled by
// applyStatus (the done tick), so skip it here.
function updateBoot(ev) {
  // The card is a COLD-START cue only: once the session is live (or gone) the technical feed
  // belongs to the run, not to startup.
  if (state.status === "ready" || state.status === "disconnected") return;
  if (ev.stage === "ready") return;
  if (ev.level === "error") { showBoot((ev.message || "Startup error").slice(0, 140), { level: "error", sub: "See the run's log, or reconnect." }); return; }
  // SSH/Duo auth: show the real message (e.g. "Duo push sent — approve on your phone").
  if (ev.stage === "ssh_auth" || ev.stage === "ssh_connect") {
    if (ev.message) showBoot(String(ev.message).slice(0, 140), { sub: BOOT_SUB });
    return;
  }
  const label = BOOT_STAGE_LABEL[ev.stage];
  if (label) showBoot(label, { sub: BOOT_SUB });
}

// ---- bring-your-own LLM API key -------------------------------------------
//
// The endpoint IS the model choice: each entry names its model (the shared model, a cluster weight
// set, a saved key's model). Switching to an API endpoint costs nothing and needs no reconnect.

const apiKeys = { creds: [], providers: [], editing: null, pendingSelect: null,
                  activeId: "", connectChoice: "", models: [], storage: null,
                  comboActive: -1, fetchedFor: "", shared: null };

// The lab's shared model on HPC3, offered as an endpoint like a saved key. Whatever a session chooses
// answers all of its calls: the cluster GPU's Qwen, the shared model, or the user's own API. A session
// on the shared model holds no GPU of its own. The server lists it (`shared` on /api/llm-credentials)
// only when it keeps one; its address and key never reach the page.
const SHARED_ENDPOINT = "lab-shared";
function sharedOptionHtml() {
  const s = apiKeys.shared;
  if (!s) return "";
  return `<option value="${SHARED_ENDPOINT}">${escapeHtml(s.label)} · ${escapeHtml(s.model)}${s.up ? "" : " (starting…)"}</option>`;
}
function isEndpointValue(v) { return v === SHARED_ENDPOINT ? !!apiKeys.shared : apiKeys.creds.some((c) => c.id === v); }
// The lab's own choices, the shared model first: where the server keeps one it is everyone's default.
function labOptionsHtml() { return sharedOptionHtml() + clusterOptionsHtml(); }
// What a picker falls back to when the endpoint in use goes away (a deleted key): the cluster GPU
// where sessions may still have one, else the shared model.
function fallbackEndpoint() { return !clusterModels.sessionGpu && apiKeys.shared ? SHARED_ENDPOINT : ""; }

// The "add one" affordance belongs INSIDE the picker, not only beside it. A first-time user opens
// the endpoint dropdown looking for their own endpoint, finds a single "Cluster GPU" entry, and
// concludes there is no such thing here — the 🔑 next to the box never says that it is where an
// endpoint comes from. This option makes the dropdown answer its own question.
const ADD_KEY_OPTION = "__add_key__";

// ---- cluster models ---------------------------------------------------------
// "Cluster GPU" is no longer one thing: HPC3 holds several weight sets (Qwen3.8-27B INT4, the
// previous Qwen3.6 AWQ, ...), each needing its own vLLM image and flags. The picker lists them as
// "cluster:<id>" options; the 🖥 dialog shows which are on disk and lets an admin manage the list.
const CLUSTER_PREFIX = "cluster:";
const clusterModels = { models: [], unregistered: null, scanned: false, canManage: false, editing: null,
                        sessionGpu: true };

function clusterValue(id) {
  const m = clusterModels.models.find((x) => x.id === id && x.enabled)
    || clusterModels.models.find((x) => x.default) || clusterModels.models[0];
  return m ? CLUSTER_PREFIX + m.id : CLUSTER_PREFIX;
}

function isClusterValue(v) { return !v || v.startsWith(CLUSTER_PREFIX); }

function clusterOptionsHtml() {
  if (!clusterModels.sessionGpu) return "";   // this server offers no session a GPU of its own
  const enabled = clusterModels.models.filter((m) => m.enabled);
  if (!enabled.length) return `<option value="${CLUSTER_PREFIX}">Cluster GPU (vLLM at UCI)</option>`;
  return enabled.map((m) => `<option value="${CLUSTER_PREFIX}${escapeHtml(m.id)}">Cluster GPU · ${escapeHtml(m.label)}${m.default ? " (default)" : ""}</option>`).join("");
}

async function loadClusterModels({ scan = false } = {}) {
  try {
    const q = scan && state.connectionId ? `?connection_id=${encodeURIComponent(state.connectionId)}` : "";
    const d = await (await fetch("/api/cluster-models" + q)).json();
    clusterModels.models = d.models || [];
    clusterModels.unregistered = d.unregistered;
    clusterModels.scanned = !!d.scanned;
    clusterModels.canManage = !!d.can_manage;
    clusterModels.sessionGpu = d.session_gpu !== false;
  } catch { /* keep the last list; the picker still works with what it has */ }
  renderEndpointSelect();
  renderConnectEndpointSelect();
  renderClusterModelList();
}
const ADD_KEY_HTML = `<option value="${ADD_KEY_OPTION}">＋ Add your own API key…</option>`;

// The owner query every credential call has to carry when accounts are off: the server then files
// keys under the UCInetID from the login form. One helper so list / probe / verify / delete can
// never disagree about whose store they are touching — they did, and a key saved under one owner
// and looked up under another is invisible in a way that looks like "the save didn't work".
function apiKeyOwner() {
  return (state.user && state.user.username) || $("userInput").value.trim() || state.hpcUser || "";
}

function apiKeyOwnerQuery() {
  const u = apiKeyOwner();
  return u ? `?user=${encodeURIComponent(u)}` : "";
}

async function loadApiCredentials() {
  try {
    const d = await (await fetch("/api/llm-credentials" + apiKeyOwnerQuery())).json();
    apiKeys.creds = d.credentials || [];
    apiKeys.shared = d.shared || null;
    apiKeys.storage = d.storage || null;
    apiKeys.sessionHours = d.session_idle_hours || 12;
  } catch { apiKeys.creds = []; }
  renderEndpointSelect();
  renderConnectEndpointSelect();
  renderApiKeyList();
  renderApiKeyStorage();
}

function renderEndpointSelect(summary) {
  const sel = $("llmEndpointSelect");
  if (!sel) return;
  const current = summary && summary.llm_endpoint;
  // The cluster list can arrive AFTER the status; remember the last endpoint the server reported so
  // a re-render then still names the model the session actually serves, not the default.
  if (current) apiKeys.lastEndpoint = current;
  const known = current || apiKeys.lastEndpoint;
  const onKey = current && (current.kind === "credential" || current.kind === "shared");
  const activeId = onKey ? current.credential_id
    : current ? clusterValue(current.cluster_model_id) : "";
  // A refresh with no status attached (we just saved or deleted a key) must not silently move the
  // selection to "Cluster GPU" — the picker would then name an endpoint that isn't the one in use.
  const keep = summary ? activeId : (sel.value && sel.value !== ADD_KEY_OPTION ? sel.value : apiKeys.activeId);
  const opts = [labOptionsHtml()];
  for (const c of apiKeys.creds) {
    const bad = c.last_error ? " ⚠" : "";
    opts.push(`<option value="${escapeHtml(c.id)}">${escapeHtml(c.label)} · ${escapeHtml(credRoles(c).pi || "no model")}${bad}</option>`);
  }
  opts.push(ADD_KEY_HTML);
  sel.innerHTML = opts.join("");
  if (summary) apiKeys.activeId = activeId;   // what to snap back to when "＋ Add" is picked
  if (keep && isEndpointValue(keep)) sel.value = keep;
  else if (keep && isClusterValue(keep) && [...sel.options].some((o) => o.value === keep)) sel.value = keep;
  else if (!known || (known.kind !== "credential" && known.kind !== "shared")) {
    const v = clusterValue(known && known.cluster_model_id);
    sel.value = [...sel.options].some((o) => o.value === v) ? v : sel.options[0].value;
  }
  if (current && onKey && !isEndpointValue(activeId)) {
    // The status names a credential we don't have in the list (another window added it, or the
    // list predates it). Reload rather than silently showing "Cluster GPU", which would misreport
    // where prompts are going.
    loadApiCredentials();
  }
  syncEndpointActions();
}

// Put the picker back on the endpoint the session is ACTUALLY using — after a switch that failed,
// or a consent card that was declined. The invariant is that this box never displays a choice that
// did not take effect; a plain re-render preserves the selection, so the snap has to be explicit.
function snapEndpointSelect() {
  const sel = $("llmEndpointSelect");
  if (!sel) return;
  sel.value = apiKeys.activeId || fallbackEndpoint() || clusterValue();
  syncEndpointActions();
}

// Edit / delete belong next to the picker, not only inside the key dialog: the endpoint you want
// to change is the one already named in the box. They act on the SELECTED row and hide themselves
// for "Cluster GPU" and for the "＋ Add" item, which are not credentials and cannot be edited.
function syncEndpointActions() {
  const pairs = [["llmEndpointSelect", "editKeyBtn", "deleteKeyBtn"],
                 ["connectLlmSelect", "connectEditKeyBtn", "connectDeleteKeyBtn"]];
  for (const [selId, editId, delId] of pairs) {
    const sel = $(selId), edit = $(editId), del = $(delId);
    if (!sel || !edit || !del) continue;
    const cred = apiKeys.creds.find((c) => c.id === sel.value);
    edit.classList.toggle("hidden", !cred);
    del.classList.toggle("hidden", !cred);
    if (cred) {
      edit.title = `Edit “${cred.label}”`;
      del.title = `Delete “${cred.label}”`;
    }
  }
}

// The picker's ✎ — open the key dialog already in edit mode for that endpoint, rather than making
// the user find its row again in a list that may be scrolled out of view.
async function editSelectedEndpoint(selId) {
  const sel = $(selId);
  if (!sel || !sel.value || sel.value === ADD_KEY_OPTION) return;
  await openApiKeys(sel.value);
}

// Deleting the endpoint a live session is USING would leave that session pointed at a credential
// that no longer exists, so switch it back to the cluster GPU first. One function for both the
// picker's 🗑 and the dialog's Delete, so neither can forget that step.
async function deleteEndpointById(credId) {
  const cred = apiKeys.creds.find((c) => c.id === credId);
  if (!cred) return;
  if (!confirm(`Delete “${cred.label}”? The stored key is removed from this server.`)) return;
  const wasActive = state.connectionId && apiKeys.activeId === cred.id;
  try {
    const res = await fetch(`/api/llm-credentials/${cred.id}${apiKeyOwnerQuery()}`, { method: "DELETE" });
    if (!res.ok) { toast("Could not delete that endpoint"); return; }
  } catch (err) {
    toast(isUnreachable(err) ? "Can't reach the local server." : "Delete failed: " + err.message);
    return;
  }
  if (wasActive) await selectLlmEndpoint(fallbackEndpoint());
  if (apiKeys.editing === cred.id) resetApiKeyForm();
  if (apiKeys.connectChoice === cred.id) apiKeys.connectChoice = "";
  await loadApiCredentials();
  toast(`Deleted “${cred.label}”`);
}

// Which model does what on a saved endpoint. Stored as `model` (the Scientist's tool loop,
// required) and `lab_model` (PI / Critic / write-up, optional, defaults to `model`); shown the
// other way round — PI & Critic first — because that is the model people mean by "which model".
function credRoles(c) {
  const sci = (c && c.model) || "";
  const pi = (c && c.lab_model) || sci;
  return { pi, sci, split: !!(pi && sci && pi !== sci) };
}

// "claude-opus-5-5" when one model does everything; "PI/Critic claude-opus-5-5 · Scientist
// claude-sonnet-5-5" when the roles are split. Escaped, for innerHTML.
function credRolesLabel(c) {
  const r = credRoles(c);
  return r.split ? `PI/Critic ${escapeHtml(r.pi)} · Scientist ${escapeHtml(r.sci)}` : escapeHtml(r.pi);
}

// A persistent banner, not a one-off toast: the session keeps sending prompts off-site for as
// long as this endpoint is selected, so the notice has to persist too.
function renderEgressBanner(summary) {
  const el = $("llmEgressBanner");
  if (!el) return;
  const ep = summary && summary.llm_endpoint;
  if (!ep || !ep.remote) { el.classList.add("hidden"); el.textContent = ""; return; }
  el.innerHTML = `🌐 Prompts leave UCI for <strong>${escapeHtml(ep.label)}</strong> (${credRolesLabel(ep)}).`
    + ` Your data files and the analysis stay on HPC3.`;
  el.classList.remove("hidden");
}

async function selectLlmEndpoint(value, { accept = false } = {}) {
  if (!state.connectionId) return;
  // A cluster value is "cluster:<id>" (or "" = whatever cluster model the session already has);
  // anything else is a saved credential id.
  const cluster = isClusterValue(value);
  const credId = cluster ? null : value;
  const clusterId = cluster && value ? value.slice(CLUSTER_PREFIX.length) || null : null;
  try {
    const res = await fetch("/api/llm-endpoint", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, credential_id: credId,
                             cluster_model_id: clusterId, accept_egress: accept }),
    });
    const d = await res.json();
    if (res.status === 409 && d.needs_consent) { openEgressDialog(credId, d); return; }
    if (!res.ok) { toast(d.error || "Could not switch endpoint"); snapEndpointSelect(); return; }
    apiKeys.activeId = credId || clusterValue(d.cluster_model_id || clusterId);
    syncEndpointActions();
    const cm = clusterModels.models.find((m) => m.id === (d.cluster_model_id || clusterId));
    toast(credId ? `LLM: ${d.credential ? d.credential.label : "your API endpoint"}`
      : d.status === "provisioning" ? `Starting ${cm ? cm.label : "the cluster model"} on a GPU — a few minutes`
      : `LLM: cluster GPU${cm ? " · " + cm.label : ""}`);
  } catch (err) {
    toast(isUnreachable(err) ? "Can't reach the local server." : "Endpoint switch failed: " + err.message);
    snapEndpointSelect();
  }
}

function openEgressDialog(credId, info) {
  apiKeys.pendingSelect = credId;
  const ep = info.endpoint || {};
  $("egressTarget").innerHTML = `<strong>${escapeHtml(ep.label || "endpoint")}</strong><br>`
    + `<code>${escapeHtml(ep.base_url || "")}</code>`;
  const li = (items) => (items || []).map((s) => `<li>${escapeHtml(s)}</li>`).join("");
  $("egressSends").innerHTML = li(info.sends);
  $("egressStays").innerHTML = li(info.stays);
  $("egressModal").classList.remove("hidden");
}

function closeEgressDialog(accepted) {
  $("egressModal").classList.add("hidden");
  // Promise mode: the connect flow awaits a decision rather than firing a follow-up request,
  // because it has to retry the SAME connect with consent attached.
  if (apiKeys.pendingResolve) {
    const resolve = apiKeys.pendingResolve;
    apiKeys.pendingResolve = null;
    apiKeys.pendingSelect = null;
    resolve(Boolean(accepted));
    return;
  }
  const credId = apiKeys.pendingSelect;
  apiKeys.pendingSelect = null;
  if (accepted && credId) selectLlmEndpoint(credId, { accept: true });
  else snapEndpointSelect();            // snap the dropdown back to what is actually in use
}

// Show the same consent card and RESOLVE with the answer. Used by connect(), which must know the
// decision before it can retry; the post-connect path stays callback-driven.
function confirmEgress(info) {
  return new Promise((resolve) => {
    apiKeys.pendingResolve = resolve;
    openEgressDialog(null, info);
  });
}

// The endpoint picker on the LOGIN form. Populated before any session exists, which is the whole
// point — choosing here is what lets connect skip the GPU.
function renderConnectEndpointSelect() {
  const sel = $("connectLlmSelect");
  if (!sel) return;
  const keep = sel.value && sel.value !== ADD_KEY_OPTION ? sel.value : apiKeys.connectChoice;
  const opts = [labOptionsHtml()];
  for (const c of apiKeys.creds) {
    opts.push(`<option value="${escapeHtml(c.id)}">${escapeHtml(c.label)} · ${escapeHtml(credRoles(c).pi || "no model")}</option>`);
  }
  opts.push(ADD_KEY_HTML);
  sel.innerHTML = opts.join("");
  if (keep && (isEndpointValue(keep) || [...sel.options].some((o) => o.value === keep))) sel.value = keep;
  apiKeys.connectChoice = sel.value;
  renderConnectLlmHint();
  syncEndpointActions();
}

// The line under the login picker is the only place that can tell a first-time user that an
// endpoint of their own is even possible, so it says what to do when they have no key yet — and
// stops nagging once they have one.
function renderConnectLlmHint() {
  const el = $("connectLlmHint");
  if (!el) return;
  const sel = $("connectLlmSelect");
  const chosen = sel && sel.value && sel.value !== ADD_KEY_OPTION && !isClusterValue(sel.value);
  if (chosen && sel.value === SHARED_ENDPOINT) {
    el.textContent = apiKeys.shared && apiKeys.shared.up === false
      ? "The lab's shared model is starting or being renewed right now, so a session on it cannot start yet. Try again later, or use your own API key."
      : "Every call of this session goes to the lab's shared model on HPC3: no GPU to wait for, nothing leaves UCI, and it costs you nothing. Your HPC3 account still runs the analysis jobs.";
  } else if (chosen) {
    el.textContent = "This session reasons on your own endpoint — no GPU queue. Your HPC3 account still runs the analysis jobs.";
  } else if (!clusterModels.sessionGpu) {
    el.textContent = "No API key saved yet. Add one here with 🔑 to use your own model; your HPC3 account still runs the analysis jobs.";
  } else if (apiKeys.shared) {
    el.textContent = "Cluster GPU runs Qwen on a card of your own for the whole session and waits for a free one, which can take hours. The shared model or your own API key start at once.";
  } else if (apiKeys.creds.length) {
    el.textContent = "Cluster GPU waits for a free card. Pick one of your own endpoints above to start in seconds — your HPC3 account still runs the analysis jobs either way.";
  } else {
    el.textContent = "Cluster GPU waits for a free card. No API key saved yet — add one here with 🔑 and a session starts in seconds; your HPC3 account still runs the analysis jobs either way.";
  }
}

// The login picker's value as a credential id: never the "＋ Add" sentinel, which is a menu item
// and would otherwise be sent to /api/connect as a credential that does not exist.
function connectEndpointChoice() {
  const sel = $("connectLlmSelect");
  const v = sel && sel.value;
  return v && v !== ADD_KEY_OPTION && !isClusterValue(v) ? v : null;
}

// The login picker's cluster model id, or null (= the lab default / an API endpoint was chosen).
function connectClusterChoice() {
  const sel = $("connectLlmSelect");
  const v = sel && sel.value;
  return v && v.startsWith(CLUSTER_PREFIX) ? v.slice(CLUSTER_PREFIX.length) || null : null;
}

// ---- cluster model manager ------------------------------------------------

async function openClusterModels() {
  $("clusterModal").classList.remove("hidden");
  $("clusterList").innerHTML = '<div class="storage-loading">Loading…</div>';
  await loadClusterModels({ scan: true });
}

function renderClusterModelList() {
  const box = $("clusterList");
  if (!box) return;
  const cm = clusterModels;
  $("clusterScanNote").textContent = cm.scanned
    ? "Disk status read just now from the shared HF cache on HPC3."
    : "Connect to HPC3 to see which weights are on disk and how big they are.";
  $("clusterAddBtn").classList.toggle("hidden", !cm.canManage);
  const inUse = apiKeys.lastEndpoint && apiKeys.lastEndpoint.kind === "cluster" ? apiKeys.lastEndpoint.cluster_model_id : null;
  if (!cm.models.length) { box.innerHTML = '<div class="storage-loading">No cluster models configured.</div>'; }
  else box.innerHTML = cm.models.map((m) => {
    const disk = !cm.scanned ? "" : m.on_disk
      ? `<span class="api-key-ok">✓ on disk${m.size_gb != null ? ` · ${m.size_gb} GB` : ""}</span>`
      : '<span class="api-key-bad">⚠ weights not in the HF cache</span>';
    const tags = [m.default ? "<strong>default</strong>" : "", m.enabled ? "" : "disabled",
                  inUse === m.id ? "in use by this session" : ""].filter(Boolean).join(" · ");
    const img = (m.image || "").split("/").pop();
    const acts = cm.canManage ? `
        ${m.default ? "" : `<button type="button" class="ghost" data-act="default">Make default</button>`}
        <button type="button" class="ghost" data-act="toggle">${m.enabled ? "Disable" : "Enable"}</button>
        <button type="button" class="ghost" data-act="edit">Edit</button>
        ${m.default ? "" : `<button type="button" class="ghost" data-act="remove">Remove</button>`}` : "";
    return `<div class="api-key-row${cm.editing === m.id ? " editing" : ""}" data-id="${escapeHtml(m.id)}">
      <div class="api-key-main">
        <strong>${escapeHtml(m.label)}</strong>${tags ? ` <small>${tags}</small>` : ""}
        <small><code>${escapeHtml(m.repo)}</code></small>
        <small>${escapeHtml(img)}${m.quantization ? " · " + escapeHtml(m.quantization) : " · quant auto"}${m.extra_args ? " · " + escapeHtml(m.extra_args) : ""} · ${m.cards ? "GPU: " + escapeHtml(m.cards) : "any GPU"}</small>
        ${m.notes ? `<small>${escapeHtml(m.notes)}</small>` : ""}
        ${disk ? `<small>${disk}</small>` : ""}
      </div>
      <div class="api-key-actions">${acts}</div>
    </div>`;
  }).join("");
  const un = $("clusterUnregistered");
  const rows = cm.unregistered || [];
  un.innerHTML = rows.length ? `<h3>On HPC3 but not in the list</h3>
    <div class="storage-items">${rows.map((r) => `<div class="api-key-row" data-repo="${escapeHtml(r.repo)}">
      <div class="api-key-main"><strong>${escapeHtml(r.repo)}</strong><small>${r.size_gb != null ? r.size_gb + " GB" : ""}</small></div>
      <div class="api-key-actions">${cm.canManage ? '<button type="button" class="ghost" data-act="register">Add</button>' : ""}</div>
    </div>`).join("")}</div>` : "";
}

function fillClusterForm(m) {
  clusterModels.editing = m && m.id ? m.id : null;
  $("clusterFormTitle").textContent = clusterModels.editing ? `Edit “${m.label}”` : "Add a model";
  $("clusterLabel").value = (m && m.label) || "";
  $("clusterRepo").value = (m && m.repo) || "";
  $("clusterRepo").readOnly = !!clusterModels.editing;
  $("clusterImage").value = (m && m.image) || "";
  $("clusterQuant").value = (m && m.quantization) || "";
  $("clusterExtra").value = (m && m.extra_args) || "";
  $("clusterCards").value = (m && m.cards) || "";
  $("clusterNotes").value = (m && m.notes) || "";
  $("clusterError").classList.add("hidden");
  $("clusterForm").classList.remove("hidden");
  renderClusterModelList();
  $("clusterLabel").focus();
}

function closeClusterForm() {
  clusterModels.editing = null;
  $("clusterForm").classList.add("hidden");
  renderClusterModelList();
}

async function clusterRequest(url, opts) {
  const res = await fetch(url, opts);
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || `HTTP ${res.status}`);
  return d;
}

async function saveClusterForm(e) {
  e.preventDefault();
  const cur = clusterModels.models.find((m) => m.id === clusterModels.editing);
  const body = { id: clusterModels.editing || null, repo: $("clusterRepo").value.trim(),
                 label: $("clusterLabel").value.trim(), image: $("clusterImage").value.trim(),
                 quantization: $("clusterQuant").value.trim(), extra_args: $("clusterExtra").value.trim(),
                 cards: $("clusterCards").value.trim(),
                 notes: $("clusterNotes").value.trim(),
                 enabled: cur ? cur.enabled : true, default: cur ? cur.default : false };
  try {
    await clusterRequest("/api/cluster-models", { method: "PUT", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify(body) });
  } catch (err) {
    $("clusterError").textContent = err.message; $("clusterError").classList.remove("hidden"); return;
  }
  closeClusterForm();
  await loadClusterModels({ scan: true });
  toast("Cluster model saved");
}

async function clusterRowAction(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const row = btn.closest(".api-key-row");
  const act = btn.dataset.act;
  if (act === "register") {
    const repo = row.dataset.repo;
    const r = (clusterModels.unregistered || []).find((x) => x.repo === repo) || { repo };
    fillClusterForm({ ...r, id: null });
    return;
  }
  const m = clusterModels.models.find((x) => x.id === row.dataset.id);
  if (!m) return;
  try {
    if (act === "edit") { fillClusterForm(m); return; }
    if (act === "default") {
      await clusterRequest(`/api/cluster-models/${encodeURIComponent(m.id)}/default`, { method: "POST" });
      toast(`New sessions now start on ${m.label}`);
    } else if (act === "toggle") {
      await clusterRequest("/api/cluster-models", { method: "PUT", headers: { "Content-Type": "application/json" },
                                                    body: JSON.stringify({ ...m, enabled: !m.enabled }) });
    } else if (act === "remove") {
      if (!confirm(`Remove “${m.label}” from the list? The weights stay on HPC3.`)) return;
      await clusterRequest(`/api/cluster-models/${encodeURIComponent(m.id)}`, { method: "DELETE" });
    }
  } catch (err) { toast(err.message); return; }
  await loadClusterModels({ scan: true });
}

// ---- credential manager ---------------------------------------------------

function renderApiKeyList() {
  const box = $("apiKeyList");
  if (!box) return;
  // Keys are stored per owner, and with accounts off the owner IS the UCInetID typed on the login
  // form. Opening this dialog before typing it would list (and save into) the anonymous "guest"
  // bucket, which the session then never looks in — so say what is missing instead of showing an
  // empty list that looks like "no keys".
  if (!apiKeyOwner()) {
    box.innerHTML = '<div class="storage-loading">Type your UCInetID on the login form first — saved keys belong to that account.</div>';
    return;
  }
  if (!apiKeys.creds.length) {
    box.innerHTML = '<div class="storage-loading">No endpoints yet. Add one below to use your own LLM account.</div>';
    return;
  }
  box.innerHTML = apiKeys.creds.map((c) => {
    const state_ = c.last_error
      ? `<span class="api-key-bad">⚠ ${escapeHtml(c.last_error)}</span>`
      : c.verified_at ? '<span class="api-key-ok">✓ verified</span>' : "<span>not tested</span>";
    const roles = c.model ? credRolesLabel(c)
      : '<span class="api-key-bad">no model — pick one before this endpoint can be used</span>';
    // Paused: no model call uses the key (the next call of a running run included) until resumed.
    // An administrator's pause can be lifted only by an administrator.
    const pausedBy = { owner: "you", admin: "an administrator", auto: "AiScientist (unusual usage)" }[c.paused_by] || "";
    const paused = c.paused_at
      ? `<small class="api-key-bad">⏸ Paused by ${escapeHtml(pausedBy)}${c.pause_reason && c.paused_by !== "owner" ? ` — ${escapeHtml(c.pause_reason)}` : ""}</small>` : "";
    const where = c.session_only
      ? ` · <span title="Held in the server's memory for this browser session only; never written to disk">this session only</span>`
      : (c.encrypted ? " 🔒" : "");
    const pauseBtn = c.paused_at
      ? (c.paused_by === "admin" ? "" : '<button type="button" class="ghost small" data-act="resume">Resume</button>')
      : '<button type="button" class="ghost small" data-act="pause" title="Stop every use of this key now, running runs included">Pause</button>';
    return `<div class="api-key-row${apiKeys.editing === c.id ? " editing" : ""}" data-id="${escapeHtml(c.id)}">
      <div class="api-key-main">
        <strong>${escapeHtml(c.label)}</strong>
        <small>${roles}</small>
        <small><code>${escapeHtml(c.base_url || "")}</code> · key ${escapeHtml(c.key_hint || "")}${where}</small>
        <small>${state_}</small>
        ${paused}
      </div>
      <div class="api-key-actions">
        ${pauseBtn}
        <button type="button" class="ghost small" data-act="test">Test</button>
        <button type="button" class="ghost small" data-act="edit">Edit / rotate key</button>
        <button type="button" class="ghost small" data-act="del">Delete</button>
      </div>
    </div>`;
  }).join("");
}

// How the key is protected, in plain words. The key never reaches the browser and never comes back
// over the wire, so this is the only place a user learns it. No server path, owner or file mode:
// they tell the user nothing and show the server's layout to everyone (issue 37).
function renderApiKeyStorage() {
  const el = $("apiKeyStorage");
  if (!el) return;
  const s = apiKeys.storage;
  if (!s) { el.textContent = ""; return; }
  el.textContent = s.separate_service
    ? `Your key is stored encrypted on the AiScientist server by a separate key service, which alone `
      + `can read it: the web server and the agents never see it. It is never in your browser and is `
      + `sent only to the endpoint you saved it for. To use a key without saving it, tick "this browser session only".`
    : `Your key is stored ${s.encrypted ? "encrypted " : ""}on the AiScientist server, `
      + `readable only by the service, never in your browser, and sent only to the endpoint you `
      + `saved it for. To use a key without saving it, tick "this browser session only".`;
}

function renderProviderOptions() {
  const sel = $("apiKeyProvider");
  if (!sel || !apiKeys.providers.length) return;
  sel.innerHTML = apiKeys.providers.map((p) =>
    `<option value="${escapeHtml(p.key)}">${escapeHtml(p.label)}</option>`).join("");
  applyProviderPreset();
}

function applyProviderPreset() {
  const p = apiKeys.providers.find((x) => x.key === $("apiKeyProvider").value);
  if (!p) return;
  $("apiKeyProviderNote").textContent = p.note || "";
  // Prefill the base URL when the field is empty OR still holds another preset's URL (it was
  // prefilled, not typed): picking OpenRouter, then Anthropic, must not leave OpenRouter's host in
  // place. A host the user typed themselves is never overwritten by flipping the dropdown.
  const field = $("apiKeyBaseUrl");
  const current = field.value.trim().replace(/\/+$/, "");
  const prefilled = !current || apiKeys.providers.some((x) => x.base_url && x.base_url.replace(/\/+$/, "") === current);
  if (prefilled) field.value = p.base_url || "";
  // Picking the provider can be what completes (base URL + key), so the model list follows either
  // order of filling the form in.
  maybeAutoFetchModels();
}

// ---- model combobox --------------------------------------------------------
//
// A model id is not something anyone knows by heart — OpenRouter alone serves ~450 — so the field
// has to answer "what do I put here?" itself. It stays a text input (endpoints that publish no
// catalogue still need one, and a picked id must stay editable) with the endpoint's own ids as a
// searchable menu under it: click to open, type to filter, click or Enter to fill.

const MODEL_COMBOS = [["apiKeyModel", "apiKeyModelMenu"], ["apiKeyLabModel", "apiKeyLabModelMenu"]];

function renderModelChoices(models) {
  apiKeys.models = models || [];
  // Refresh whichever menu is open so a fetch that lands while the user is looking at it fills in
  // rather than leaving "press ↻" under their cursor.
  for (const [inputId, menuId] of MODEL_COMBOS) {
    if (!$(menuId).classList.contains("hidden")) openModelMenu(inputId, menuId);
  }
}

function matchingModels(query) {
  const q = (query || "").trim().toLowerCase();
  if (!q) return apiKeys.models;
  return apiKeys.models.filter((m) => m.toLowerCase().includes(q));
}

function openModelMenu(inputId, menuId) {
  const input = $(inputId), menu = $(menuId);
  if (!input || !menu) return;
  const q = input.value.trim();
  const hits = matchingModels(q);
  apiKeys.comboActive = -1;

  if (!apiKeys.models.length) {
    // Nothing fetched yet — say what would fetch it, and make the line itself do it, because a
    // user who does not know what to type also does not know what the ↻ glyph is for.
    menu.innerHTML = '<div class="combo-note clickable" data-act="fetch">Press ↻ to list what this endpoint serves</div>';
  } else if (!hits.length) {
    menu.innerHTML = `<div class="combo-note">No model here matches “${escapeHtml(q)}” · ${apiKeys.models.length} available</div>`;
  } else {
    // Cap the rendered rows: 450 ids is a scroll bar, not a choice. Typing narrows it.
    const shown = hits.slice(0, 60);
    menu.innerHTML = shown.map((m) => `<div class="combo-item" role="option" data-v="${escapeHtml(m)}">${highlightMatch(m, q)}</div>`).join("")
      + (hits.length > shown.length
         ? `<div class="combo-note">+${hits.length - shown.length} more — keep typing to narrow</div>` : "");
  }
  menu.classList.remove("hidden");
  input.setAttribute("aria-expanded", "true");
}

function closeModelMenu(inputId, menuId) {
  const menu = $(menuId);
  if (!menu) return;
  menu.classList.add("hidden");
  $(inputId).setAttribute("aria-expanded", "false");
  apiKeys.comboActive = -1;
}

// Show WHERE the typed text matched, so a filtered list of near-identical ids is readable.
function highlightMatch(text, query) {
  const q = (query || "").trim();
  if (!q) return escapeHtml(text);
  const at = text.toLowerCase().indexOf(q.toLowerCase());
  if (at < 0) return escapeHtml(text);
  return escapeHtml(text.slice(0, at)) + `<mark>${escapeHtml(text.slice(at, at + q.length))}</mark>`
    + escapeHtml(text.slice(at + q.length));
}

function moveComboActive(menuId, delta) {
  const items = [...$(menuId).querySelectorAll(".combo-item")];
  if (!items.length) return;
  const next = apiKeys.comboActive + delta;
  apiKeys.comboActive = next < 0 ? items.length - 1 : next >= items.length ? 0 : next;
  items.forEach((el, i) => el.classList.toggle("active", i === apiKeys.comboActive));
  items[apiKeys.comboActive].scrollIntoView({ block: "nearest" });
}

function wireModelCombo(inputId, menuId) {
  const input = $(inputId), menu = $(menuId);
  if (!input || !menu) return;

  input.addEventListener("focus", () => openModelMenu(inputId, menuId));
  input.addEventListener("input", () => openModelMenu(inputId, menuId));
  // Click as well as focus: clicking a field that ALREADY has focus fires no focus event, so
  // without this the menu stays shut for anyone who dismissed it and clicked the box again —
  // which reads as the box being broken.
  input.addEventListener("click", () => openModelMenu(inputId, menuId));
  input.addEventListener("keydown", (e) => {
    const open = !menu.classList.contains("hidden");
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) { openModelMenu(inputId, menuId); return; }
      moveComboActive(menuId, e.key === "ArrowDown" ? 1 : -1);
    } else if (e.key === "Enter" && open) {
      // Enter completes what is highlighted; with nothing highlighted it takes the top match,
      // because a half-typed id is never what the user meant to save and the endpoint would
      // refuse it anyway. Enter still submits the form when the text IS a real model id, or when
      // there is nothing to complete it with (a custom id, or an endpoint that lists nothing) —
      // and Escape closes the menu, so submitting exactly what you typed is always available.
      const items = menu.querySelectorAll(".combo-item");
      const el = items[apiKeys.comboActive >= 0 ? apiKeys.comboActive : 0];
      if (el && !apiKeys.models.includes(input.value.trim())) {
        e.preventDefault();
        input.value = el.dataset.v;
        closeModelMenu(inputId, menuId);
      }
    } else if (e.key === "Escape" && open) {
      e.preventDefault(); e.stopPropagation();      // close the menu, not the whole dialog
      closeModelMenu(inputId, menuId);
    }
  });
  // mousedown, not click: the input's blur fires first and would hide the menu out from under
  // the pointer, so the click would land on nothing.
  menu.addEventListener("mousedown", (e) => {
    const note = e.target.closest('[data-act="fetch"]');
    if (note) { e.preventDefault(); fetchEndpointModels(); return; }
    const item = e.target.closest(".combo-item");
    if (!item) return;
    e.preventDefault();
    input.value = item.dataset.v;
    $("apiKeyError").classList.add("hidden");
    closeModelMenu(inputId, menuId);
  });
  input.addEventListener("blur", () => setTimeout(() => closeModelMenu(inputId, menuId), 120));
}

function resetApiKeyForm() {
  apiKeys.editing = null;
  $("apiKeyFormTitle").textContent = "Add an endpoint";
  $("apiKeyHintLabel").textContent = "required";
  $("apiKeySave").textContent = "Verify & save";
  $("apiKeyCancel").classList.add("hidden");
  for (const id of ["apiKeyBaseUrl", "apiKeyValue", "apiKeyModel", "apiKeyLabModel", "apiKeyLabel"]) $(id).value = "";
  $("apiKeySessionOnly").checked = false;
  $("apiKeySessionRow").classList.remove("hidden");
  $("apiKeySessionHint").textContent = `kept in the server's memory until you log out or leave it unused for ${apiKeys.sessionHours || 12} hours`;
  apiKeys.fetchedFor = "";
  renderModelChoices([]);
  for (const [inputId, menuId] of MODEL_COMBOS) closeModelMenu(inputId, menuId);
  $("apiKeyError").classList.add("hidden");
  applyProviderPreset();
  renderApiKeyList();   // clears the "editing" marker on whichever row had it
}

function editApiKey(cred) {
  apiKeys.editing = cred.id;
  $("apiKeyFormTitle").textContent = `Edit “${cred.label}”`;
  // The stored key is never sent back to the browser, so an empty field here means "keep the
  // current key" rather than "no key" — which is also exactly what a rotation needs. Providers
  // show a key once and never again, so say plainly that this edit does not need it.
  $("apiKeyHintLabel").textContent =
    `keeping the saved key ${cred.key_hint || ""} — paste one only to replace it`;
  $("apiKeySave").textContent = "Save changes";
  $("apiKeyCancel").classList.remove("hidden");
  $("apiKeySessionRow").classList.add("hidden");     // saved or session-only is chosen when adding
  $("apiKeyProvider").value = cred.provider || "custom";
  $("apiKeyBaseUrl").value = cred.base_url || "";
  $("apiKeyValue").value = "";
  // Top field = PI & Critic; the Scientist's field stays empty unless it really is a different model.
  const roles = credRoles(cred);
  $("apiKeyLabModel").value = roles.pi;
  $("apiKeyModel").value = roles.split ? roles.sci : "";
  $("apiKeyLabel").value = cred.label || "";
  $("apiKeyError").classList.add("hidden");
  $("apiKeyProviderNote").textContent = "";
  apiKeys.fetchedFor = "";
  renderModelChoices([]);
  // The key is already on file, so nothing is needed from the user to answer "what can I put in
  // Model?" — ask the endpoint now and have the list waiting when they click the box.
  maybeAutoFetchModels();
  renderApiKeyList();                       // mark which row this form now belongs to
  $("apiKeyForm").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function apiKeyError(msg, cause) {
  // Each cause has its own fix, so name the fix rather than repeating the error.
  const fix = {
    auth: "Check the key was copied whole, and that it belongs to this provider.",
    credit: "The key works but the account is out of credit.",
    rate: "The key works but is rate-limited — wait a minute, or raise the account's limits with the provider.",
    model: "That model id isn't served here — press ↻ to list what is.",
    endpoint: "Check the base URL. It should end in /v1 for most providers.",
    network: "The server could not reach that host.",
  }[cause];
  const el = $("apiKeyError");
  el.textContent = fix ? `${msg} — ${fix}` : msg;
  el.classList.remove("hidden");
}

async function submitApiKey(ev) {
  ev.preventDefault();
  // The form asks for the PI & Critic model first and the Scientist's second (blank = the same);
  // the store wants the Scientist's as `model` and the PI's as `lab_model` only when they differ.
  const piModel = $("apiKeyLabModel").value.trim();
  const sciModel = $("apiKeyModel").value.trim();
  const body = {
    provider: $("apiKeyProvider").value,
    base_url: $("apiKeyBaseUrl").value.trim(),
    model: sciModel || piModel,
    api_key: $("apiKeyValue").value.trim(),
    lab_model: sciModel && sciModel !== piModel ? piModel : "",
    label: $("apiKeyLabel").value.trim() || null,
    user: apiKeyOwner(),
  };
  const editing = apiKeys.editing;
  if (!editing && $("apiKeySessionOnly").checked) body.session_only = true;
  if (!editing && !body.api_key) { apiKeyError("Paste an API key to save this endpoint."); return; }
  // A credential with no model id is refused at bind time ("Set a model on this credential"),
  // so saving one produces an endpoint that appears in the picker and then cannot be used.
  // Catch it here, where ↻ is one button away, instead of at the start of a run.
  if (!piModel) {
    apiKeyError("Choose the model for PI & Critic — press ↻ to list what this endpoint serves.");
    return;
  }
  // Without an owner the server files this under "guest" and the session — which looks it up by
  // UCInetID — never finds it again. A key that verifies and then silently isn't there is worse
  // than a refused save, so refuse.
  if (!body.user) {
    apiKeyError("Type your UCInetID on the login form first — saved keys belong to that account.");
    return;
  }

  $("apiKeySave").disabled = true;
  $("apiKeySave").textContent = "Verifying…";
  try {
    const res = await fetch(editing ? `/api/llm-credentials/${editing}` : "/api/llm-credentials", {
      method: editing ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const d = await res.json();
    if (!res.ok) { apiKeyError(d.error || "Could not save", d.cause); return; }
    $("apiKeyValue").value = "";
    const saved = d.credential;
    // An edit that changed the endpoint is re-tested server-side with the stored key. Saying so
    // matters: a model id the provider does not serve otherwise saves quietly and only fails
    // later, in the middle of a run.
    const reverify = d.verify;
    resetApiKeyForm();
    await loadApiCredentials();
    if (saved && !editing) {
      // The key was added FOR the picker the user came from: select it there, so they close the
      // dialog onto the choice they just made rather than back on "Cluster GPU".
      if (state.connectionId) {
        await selectLlmEndpoint(saved.id);        // may ask for egress consent first
      } else if ($("connectLlmSelect")) {
        apiKeys.connectChoice = saved.id;
        $("connectLlmSelect").value = saved.id;
        renderConnectLlmHint();
        syncEndpointActions();
      }
    }
    if (reverify && !reverify.ok) toast(`Saved, but ${reverify.message}`);
    else toast(editing ? "Endpoint updated" : body.session_only ? "Key ready for this session (not saved)" : "Endpoint saved");
  } catch (err) {
    apiKeyError(isUnreachable(err) ? "Can't reach the local server." : err.message);
  } finally {
    // Restore the button for whatever mode the form is in NOW — a failed edit leaves us still
    // editing, and the label used to stay stuck on "Verifying…" with no way back.
    $("apiKeySave").disabled = false;
    $("apiKeySave").textContent = apiKeys.editing ? "Save changes" : "Verify & save";
  }
}

// ↻ — list what this endpoint serves. Works on the form as typed, with nothing saved yet: a
// credential is refused at bind time unless it names a model, and the ids live on the provider,
// so "save first, then list" was a cycle whose first lap always produced a dead endpoint.
// Fetch without being asked, as soon as the form holds enough to fetch with — a saved endpoint
// being edited (key on file) or a base URL and key both filled in. Silent: a half-finished form
// must not throw errors at someone who has not asked for anything yet. Once per input pair, so
// tabbing through the form does not re-ask the provider.
function maybeAutoFetchModels() {
  const cred = apiKeys.editing ? apiKeys.creds.find((c) => c.id === apiKeys.editing) : null;
  const baseUrl = $("apiKeyBaseUrl").value.trim();
  const typedKey = $("apiKeyValue").value.trim();
  if (!baseUrl) return;
  if (!typedKey && !(cred && (cred.base_url || "") === baseUrl)) return;
  const token = `${baseUrl}|${typedKey ? typedKey.slice(-6) : "stored:" + (cred ? cred.id : "")}`;
  if (apiKeys.fetchedFor === token) return;
  apiKeys.fetchedFor = token;
  fetchEndpointModels({ silent: true });
}

async function fetchEndpointModels({ silent = false } = {}) {
  const cred = apiKeys.editing ? apiKeys.creds.find((c) => c.id === apiKeys.editing) : null;
  const typedKey = $("apiKeyValue").value.trim();
  const baseUrl = $("apiKeyBaseUrl").value.trim();
  const fail = (msg, cause) => { if (!silent) apiKeyError(msg, cause); };
  if (!baseUrl) { fail("Enter the base URL first — that is where the model list comes from.", "endpoint"); return; }
  // With no key typed we can fall back to the one already stored for this credential, but the
  // server will then use the credential's OWN base URL (it must not aim a stored key at a host
  // supplied in the request). So if the URL was edited, the key has to come with it.
  const useStored = !typedKey && cred;
  if (useStored && (cred.base_url || "") !== baseUrl) {
    fail(`Paste the key for that base URL — the key on file belongs to ${cred.base_url}.`);
    return;
  }
  if (!typedKey && !cred) { fail("Paste your API key first — listing models needs it.", "auth"); return; }

  const btn = $("apiKeyFetchModels");
  btn.disabled = true;
  try {
    const res = await fetch("/api/llm-models", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_url: baseUrl, api_key: typedKey, provider: $("apiKeyProvider").value,
                             credential_id: useStored ? cred.id : "", user: apiKeyOwner() }),
    });
    const d = await res.json();
    if (!res.ok) { renderModelChoices([]); fail(d.error || "Could not list models", d.cause); return; }
    renderModelChoices(d.models || []);
    if (!silent) {
      // Asked for explicitly: show the answer rather than leaving the user to guess that the
      // closed box behind the button now knows something.
      $("apiKeyError").classList.add("hidden");
      openModelMenu("apiKeyLabModel", "apiKeyLabModelMenu");
      $("apiKeyLabModel").focus();
      toast(`${(d.models || []).length} models — type to filter`);
    }
  } catch (err) {
    fail(isUnreachable(err) ? "Can't reach the local server." : err.message);
  } finally { btn.disabled = false; }
}

// One of the user's API keys was paused (an administrator, or the spike check) or its usage spiked
// (key_guard). Said once as a toast, and the key list redrawn so the row shows why.
function onKeyNotice(msg) {
  const label = msg.label || "your API key";
  const text = msg.type === "key_spike"
    ? `Unusual usage of ${label}: ${msg.reason || ""}. Check it in your provider's console.`
    : msg.kind === "admin"
      ? `An administrator paused ${label}${msg.reason ? ` (${msg.reason})` : ""}. No run uses it until an administrator resumes it.`
      : `${label} was paused: ${msg.reason || "unusual usage"}. If this usage was not yours, rotate the key at the provider; if it was, resume it under API endpoints.`;
  toast(text);
  loadApiCredentials();
}

async function apiKeyRowAction(ev) {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.closest(".api-key-row").dataset.id;
  const cred = apiKeys.creds.find((c) => c.id === id);
  if (!cred) return;
  const q = apiKeyOwnerQuery();

  if (btn.dataset.act === "edit") { editApiKey(cred); return; }
  if (btn.dataset.act === "del") { await deleteEndpointById(id); return; }
  if (btn.dataset.act === "pause" || btn.dataset.act === "resume") {
    btn.disabled = true;
    try {
      const res = await fetch(`/api/llm-credentials/${id}/${btn.dataset.act}${q}`, { method: "POST" });
      const d = await res.json();
      toast(res.ok ? `${cred.label}: ${btn.dataset.act === "pause" ? "paused — no run uses it until you resume it" : "resumed"}`
        : (d.error || "Could not change the key"));
      await loadApiCredentials();
    } finally { btn.disabled = false; }
    return;
  }
  btn.disabled = true;
  btn.textContent = "Testing…";
  try {
    const d = await (await fetch(`/api/llm-credentials/${id}/verify${q}`, { method: "POST" })).json();
    toast(d.result && d.result.ok ? `${cred.label}: working` : `${cred.label}: ${(d.result || {}).message || "failed"}`);
    await loadApiCredentials();
  } finally { btn.disabled = false; btn.textContent = "Test"; }
}

async function openApiKeys(editId) {
  $("apiKeysModal").classList.remove("hidden");
  if (!apiKeys.providers.length) {
    try { apiKeys.providers = (await (await fetch("/api/llm-providers")).json()).providers || []; } catch {}
    renderProviderOptions();
  }
  resetApiKeyForm();
  await loadApiCredentials();
  // Opened from a picker's ✎ — land directly on that endpoint's form instead of making the user
  // find its row again.
  const cred = editId && apiKeys.creds.find((c) => c.id === editId);
  if (cred) editApiKey(cred);
}

function renderGpu(summary) {
  const h = summary.gpu && summary.gpu.health;
  const node = summary.gpu && summary.gpu.node;
  if (!h) { $("connGpu").textContent = node ? `GPU ${node}` : "GPU —"; return; }
  // Build the GPU line as discrete " · "-joined segments, keeping the node id and the memory
  // figure UNBREAKABLE — their hyphens / slash are otherwise wrap opportunities that split
  // "hpc3-gpu-l54-05" mid-token in the narrow sidebar. The row then wraps cleanly at the
  // separators only, and the pill stays on the first line (see .conn-gpu in the CSS).
  const nowrap = (s) => `<span class="nowrap">${escapeHtml(s)}</span>`;
  const seg = [
    escapeHtml(h.name || "GPU"),
    node ? nowrap(node) : "",
    `${h.util_percent}%`,
    h.mem_total_mb ? nowrap(`${(h.mem_used_mb / 1024).toFixed(1)}/${(h.mem_total_mb / 1024).toFixed(0)}GB`) : "",
  ].filter(Boolean).join(" · ");
  $("connGpu").innerHTML = h.healthy
    ? `<span class="gpu-pill ok"></span><span class="conn-gpu-txt">${seg}</span>`
    : `<span class="gpu-pill bad"></span><span class="conn-gpu-txt">GPU link down — ${escapeHtml((h.reason || "").slice(0, 60))}</span>`;
}

// ---- duo ------------------------------------------------------------------

function showDuoPanel(prompt) {
  $("duoPrompt").textContent = prompt || "Duo two-factor — enter a passcode or choose an option.";
  $("duoPanel").classList.remove("hidden");
  $("duoInput").value = "";
  $("duoInput").focus();
}
function hideDuoPanel() { $("duoPanel").classList.add("hidden"); }

async function submitDuo(value) {
  if (!state.connectionId) return;
  try {
    await fetch("/api/auth/duo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connection_id: state.connectionId, response: value }) });
    hideDuoPanel();
    toast("Duo response sent — waiting for the server…");
  } catch (err) { toast(isUnreachable(err) ? "Can't reach the local server." : "Duo submit failed: " + err.message); }
}

// ---- websocket ------------------------------------------------------------

function openWs(id) {
  // Epoch tags this socket: only the CURRENT socket drives auto-reconnect, so a
  // superseded one (we opened a newer socket, or the user disconnected) stays closed.
  const epoch = (state.wsEpoch || 0) + 1;
  state.wsEpoch = epoch;
  if (state.ws) { try { state.ws.close(); } catch {} }
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws/${id}`);
  ws.onopen = () => {
    if (state.wsEpoch !== epoch) return;
    const back = (state.wsRetries || 0) > 0;         // a reconnect: the server can be reached again
    state.wsRetries = 0;
    if (back) uploadsLinkBack();
  };
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === "status" && msg.connection && msg.connection.gpu) {
      state.lastJobId = msg.connection.gpu.job_id;
      state.lastNode = msg.connection.gpu.node;
      state.lastGres = msg.connection.gpu.gres;
    }
    handleWsMessage(msg);
  };
  ws.onerror = () => {};   // errors surface as onclose; the reconnect below handles them
  ws.onclose = (ev) => {
    if (state.wsEpoch !== epoch || state.connectionId !== id) return;
    // 4401/4403/4404 = the server refused this connection id (logged out, another account's, or
    // gone). Retrying cannot change that, so forget the id instead of reconnecting forever.
    if (ev && ev.code >= 4400 && ev.code < 4500) {
      forgetConnection();
      if (ev.code === 4401) showLogin();
      return;
    }
    // The server-side Connection + run task OUTLIVE a dropped socket (network blip,
    // laptop sleep, idle timeout). Reconnect with backoff so the WS endpoint replays:
    // continuing a live run, or RECOVERING one that finished while we were offline —
    // no manual page refresh needed.
    const delay = Math.min(1000 * 2 ** (state.wsRetries || 0), 15000);
    state.wsRetries = (state.wsRetries || 0) + 1;
    setTimeout(() => { if (state.wsEpoch === epoch && state.connectionId === id) openWs(id); }, delay);
  };
  state.ws = ws;
}

// Stream-family events that belong to a chat's in-flight (or recovering) assistant turn. When a
// duplicate finished-run replay is suppressed, every one of these is a no-op for that chat until
// its next chat_start clears the flag.
const STREAM_TYPES = new Set([
  "chat_token", "chat_thinking", "lab_progress", "run_status", "step_code",
  "chat_done", "chat_stopped", "chat_error", "artifacts", "run_complete",
]);

// Run-SCOPED events (a specific run's bubble, plan card, or decision prompt). One SSH/GPU
// Connection is shared across a user's chats, windows and tabs, and several chats may each have a
// run in flight, so the server tags every one of these with the conversation that OWNS the run.
// Each goes to that chat's record — and only if this window owns the chat's run, so another
// window's run never streams into, cancels, or shows a plan card in this one.
const RUN_SCOPED = new Set([...STREAM_TYPES,
  "chat_start", "plan_prompt", "plan_clarify", "decision_prompt", "plan_done",
  "plan_status", "run_usage", "confirm_request", "confirm_done",
  // Context occupancy belongs to the conversation that asked, so another chat's turn never
  // rewrites this one's meter. Deliberately NOT in STREAM_TYPES: it is a standing status, not
  // part of the assistant bubble being replayed.
  "chat_context"]);

// The chat a run event is for. An untagged event (an older gateway) goes to this window's one chat
// with a run in flight — or, with none in flight, to the only chat that launched a run here (a
// follow-up question comes before its run starts).
function chatForEvent(msg) {
  if (msg.conversation_id) {
    // Saved plans belong to the account's chat, including on a new browser after a restart.
    if ((msg.type === "plan_prompt" || msg.type === "plan_clarify") && msg.saved
        && state.sessions.some((s) => String(s.id) === String(msg.conversation_id))) {
      const c = chatOf(String(msg.conversation_id));
      c.owned = true;
      return c;
    }
    const c = chats.get(String(msg.conversation_id));
    return c && c.owned ? c : null;
  }
  const owned = [...chats.values()].filter((c) => c.owned);
  const live = owned.filter((c) => c.running || c.planPending || c.run);
  if (live.length === 1) return live[0];
  return !live.length && owned.length === 1 ? owned[0] : null;
}

function handleWsMessage(msg) {
  if (RUN_SCOPED.has(msg.type)) {
    const c = chatForEvent(msg);
    if (c) handleRunEvent(c, msg);
    return;
  }
  switch (msg.type) {
    case "status": applyStatus(msg.connection); break;
    case "session_state": renderSessionState(msg.session); break;   // the session's copies and state on HPC3
    case "event": appendLog(msg); updatePipelineFromEvent(msg); updateBoot(msg); break;
    case "gpu_health": renderGpu({ gpu: { health: msg.health, job_id: state.lastJobId, node: state.lastNode, gres: state.lastGres } }); break;
    case "upload_staging": onStagingEvent(msg.job); break;       // a copy to HPC3 moved on
    case "datasets_changed": onDatasetsChanged(); break;         // a dataset moved from HPC3 to the server
    case "result_saved": onResultSaved(msg); break;              // a result copied back from HPC3 is saved
    case "usage_update": onUsageUpdate(msg); break;              // a model call was recorded
    case "key_paused": case "key_spike": onKeyNotice(msg); break; // one of your API keys was paused
    case "duo_prompt": showDuoPanel(msg.prompt); break;
    case "duo_done": hideDuoPanel(); break;
    case "error": toast(msg.message); break;
  }
}

function handleRunEvent(c, msg) {
  if (msg.type === "chat_start") {
    // Drop a duplicate replay of a turn this browser already saved: `recover` = the server is
    // replaying a turn that already FINISHED, for a client that may have missed its live
    // completion. Otherwise open the live bubble for the chat's run (or recover a missed one into
    // that chat + downloads).
    c.suppress = !!(msg.recover && (alreadyHaveRun(msg.run_id) || seenStream(msg.stream_id)));
    if (c.suppress) return;
    setChatRunning(c, true);
    startAssistantStream(c);
    c.answered = null;
    c.run.runId = msg.run_id || null;
    c.run.streamId = c.lastStreamId = msg.stream_id || null;
    return;
  }
  if (c.suppress && STREAM_TYPES.has(msg.type)) return;
  switch (msg.type) {
    case "chat_token": streamToken(c, msg.token); break;
    case "chat_thinking": streamThinking(c, msg.token); break;
    case "lab_progress": streamProgress(c, msg); break;
    case "run_status": streamStatus(c, msg.text); break;
    case "step_code": streamCode(c, msg.code); break;
    case "chat_context": c.ctx = msg; if (chatVisible(c)) renderContextMeter(msg); break;
    case "run_usage":
      if (msg.actual) {
        pushToSession(c.sid, { role: "assistant", content: runUsageMarkdown(msg),
          streamId: `usage:${msg.run_id || msg.stream_id || c.lastStreamId}` });
        if (chatVisible(c)) renderChat();
      }
      break;
    case "chat_done": finishAssistantStream(c); setChatRunning(c, false); renderChatUsage(); checkBudgetNotices(); break;
    case "chat_stopped":
      if (msg.awaiting_plan) {
        finishAssistantTurn(c, true); setChatRunning(c, false);
        break;
      }
      if (c.run && !c.run.text) c.run.text = "_⏹ Stopped._";
      finishAssistantStream(c); setChatRunning(c, false);
      toast(chatVisible(c) ? "Run stopped — compute released." : `Run stopped in “${chatTitle(c.sid)}”.`); break;
    case "chat_error":
      if (c.run) c.run.text = `**Chat error:** ${msg.message || ""}`;
      finishAssistantStream(c); setChatRunning(c, false);
      toast(chatVisible(c) ? "Chat error" : `Chat error in “${chatTitle(c.sid)}”`); break;
    case "artifacts": appendArtifacts(c, msg.items || [], msg.bundle_url); break;
    case "run_complete":
      if (msg.run_id) { state.lastRunId = msg.run_id; try { localStorage.setItem(LASTRUN_KEY, msg.run_id); } catch {} }
      if (!chatVisible(c)) toast(`“${chatTitle(c.sid)}” finished.`);
      break;
    // The three prompts that wait for the user are held in the chat's record first and drawn from
    // there (holdPlanPrompt): only into their own chat, and again whenever that chat is rebuilt.
    case "plan_prompt":
      if (msg.saved) setChatRunning(c, false);
      holdPlanPrompt(c, "plan", { agenda: msg.agenda || [], doc: msg.plan || null, problems: msg.problems || [],
                                    estimate: msg.estimate || null }, msg);
      break;
    case "plan_clarify":
      if (msg.saved) setChatRunning(c, false);
      holdPlanPrompt(c, "clarify", { questions: msg.questions || [] }, msg); break;
    case "decision_prompt": holdPlanPrompt(c, "decision", { goal: msg.goal || "", options: msg.options || [] }, msg); break;
    case "plan_done": hidePlanPanel(c); break;
    case "plan_status": if (chatVisible(c)) setPlanStatus(msg.text); break;
    case "confirm_request": holdConfirm(c, msg); break;
    case "confirm_done": releaseConfirm(c, msg); break;
  }
}

// ---- actions --------------------------------------------------------------

async function connect(e) {
  e.preventDefault();
  const useKey = $("authMethod").value === "ssh_key";
  const body = {
    ucinetid: $("userInput").value.trim(),
    password: useKey ? null : $("passwordInput").value,
    auth_method: $("authMethod").value,
    duo_method: $("duoMethod") ? $("duoMethod").value : "push",
    credential_id: useKey ? ($("credSelect").value || null) : null,
    create_key: !useKey && $("createKeyCheck") ? $("createKeyCheck").checked : false,
    // one field, two uses: passphrase to UNLOCK a saved encrypted key, or to ENCRYPT a
    // newly-minted key on a password+Duo login.
    key_passphrase: (useKey ? $("keyPassInput").value : ($("newKeyPassInput") && $("newKeyPassInput").value)) || null,
    host: $("hostInput").value.trim(),
    mock: $("mockCheck").checked,
    // Chosen on THIS form, so the server knows before it provisions: with your own endpoint it
    // skips the GPU allocation entirely instead of queueing for a card nothing will use.
    llm_credential_id: connectEndpointChoice(),
    cluster_model_id: connectClusterChoice(),
    accept_egress: false,
  };
  if (!body.mock && !body.ucinetid) { toast("Enter your UCInetID."); return; }
  if (useKey && !body.credential_id && !body.mock) { toast("No saved SSH key — log in with password + Duo once first."); return; }
  const rh = $("reattachHint"); if (rh) rh.classList.add("hidden");  // hint served its purpose
  resetPipeline();
  const ls = $("logStream"); if (ls) ls.innerHTML = "";
  $("connectBtn").disabled = true;
  try {
    let res = await fetch("/api/connect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let data = await res.json();
    // 409 + needs_consent: the chosen endpoint is outside UCI and this user has not yet
    // acknowledged what leaves. Ask, then retry the SAME connect — the server records the consent
    // on the credential, so it is asked once per endpoint, not once per session.
    if (res.status === 409 && data.needs_consent) {
      $("connectBtn").disabled = false;
      if (!(await confirmEgress(data))) return;
      body.accept_egress = true;
      $("connectBtn").disabled = true;
      res = await fetch("/api/connect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      data = await res.json();
    }
    if (!res.ok) { toast(data.error || "Connect failed"); $("connectBtn").disabled = false; return; }
    // One session per account: a second window joins the one already open (same chats, same model).
    if (data.joined) toast("Joined your session that is already open in another window.");
    state.connectionId = data.connection_id;
    try { localStorage.setItem(CONN_KEY, data.connection_id); } catch {}
    openWs(data.connection_id);
  } catch (err) {
    toast(isUnreachable(err) ? "Can't reach the local console server — is it running?" : "Connect failed: " + err.message);
  } finally {
    $("connectBtn").disabled = false;
  }
}

async function doStopGpu() {
  try {
    const res = await fetch("/api/stop-gpu", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connection_id: state.connectionId }) });
    const d = await res.json();
    if (!res.ok) { toast(d.error || "Stop failed"); return false; }
    // The backend answers 200 with {status:"no_job"} and NO job_id when squeue found nothing to
    // cancel. That used to fall through to the success toast below and render literally
    // "GPU job undefined cancelled — SU charge stopped." — telling the user they had killed a job
    // that was never found. Report it honestly and do NOT count it as a confirmed stop.
    if (d.status === "no_job" || !d.job_id) {
      toast(d.message || "No running GPU job found for you — nothing was cancelled.");
      return false;
    }
    // Confirmed dead (the backend re-queried squeue to prove it) — drop the remembered session so the
    // login screen can never again tell the user this job is "still running".
    try { localStorage.removeItem(LASTCONN_KEY); } catch {}
    toast(`GPU job ${(d.job_ids || [d.job_id]).join(", ")} cancelled — SU charge stopped.`);
    return true;
  } catch (err) {
    toast(isUnreachable(err) ? "Can't reach the local server. Verify any real GPU job with `squeue --me` on HPC3." : "Stop failed: " + err.message);
    return false;
  }
}

// ---- the compute session on HPC3 (RCHENLAB/AiScientist issue 6) ------------------------------
// What this session is doing with HPC3 (staging in, running, staging out, cleaning up, closed,
// failed), each copy's progress and every failure, from the server's `session_state` pushes and the
// status summary after a reload. My account → Storage lists the account's recent sessions.
const SESSION_STATE_LABEL = {
  open: "Open", "staging-in": "Staging in", running: "Running", "staging-out": "Staging out",
  "cleaning-up": "Cleaning up", closed: "Closed", failed: "Failed",
};
const SESSION_STATE_SHORT = { "staging-in": "In", "staging-out": "Out", "cleaning-up": "Cleanup", failed: "Failed" };

// One copy, in words: what it copies and how far it is.
function transferText(t) {
  const total = t.bytes_total || 0;
  if (t.status === "running") {
    if (!total) return `${t.label}: starting…`;
    const pct = t.percent != null ? t.percent : Math.round(100 * (t.bytes_done || 0) / total);
    const files = t.files_total > 1
      ? ` · file ${Math.min((t.files_done || 0) + 1, t.files_total)} of ${t.files_total}` : "";
    return `${t.label}: ${fmtSize(t.bytes_done || 0)} of ${fmtSize(total)} (${pct}%)${files}`;
  }
  if (t.status === "failed") return `${t.label}: failed (${t.error || "unknown error"})`;
  if (t.status === "cancelled") return `${t.label}: stopped`;
  return `${t.label}: done` + (total ? ` (${fmtSize(total)})` : ", already there");
}

// How an ended session's HPC3 files went.
function cleanupText(c, ttlDays) {
  const days = ttlDays || 3;
  if (!c) return "";
  const kept = (c.kept_unsaved || []).length
    ? ` Kept because a result was not saved: run ${c.kept_unsaved.join(", ")}.` : "";
  switch (c.status) {
    case "queued": return `Cleanup job ${c.job_id} queued on HPC3.` + kept;
    case "done": return `Cleanup job ${c.job_id} finished` +
      (c.removed != null ? `: ${c.removed} folder(s) removed.` : ".") + kept;
    case "nothing": return "Nothing of it was left on HPC3." + kept;
    case "failed": case "error": return `Cleanup: ${c.error || "failed"}. Its files go with the ${days}-day sweep.` + kept;
    case "skipped":
      if (c.reason === "mock") return "A mock session: nothing on HPC3.";
      if (c.reason === "sweep") return `Ended without a disconnect: its HPC3 files go with the ${days}-day sweep.` + kept;
      return `Its HPC3 files go with the ${days}-day sweep.` + kept;
    default: return "";
  }
}

// The one line under the state: what is happening, or how it ended.
function sessionHeadline(s, ttlDays) {
  const st = s.state || "open";
  const unresolved = s.unresolved || [];
  if (st === "failed" && unresolved.length) {
    const t = unresolved[unresolved.length - 1];
    return `${t.label} failed: ${String(t.error || "unknown error").replace(/\.+$/, "")}. It is still on HPC3` +
      (s.closed_at ? `, kept for ${ttlDays || 3} days: connect and save it again from Runs.` : ": try again from Runs.");
  }
  if (s.closed_at) return cleanupText(s.cleanup, ttlDays);
  return ({
    open: "Connected; nothing is being copied.",
    "staging-in": "Copying a run's inputs from the server to HPC3.",
    running: "A run is executing on HPC3.",
    "staging-out": "Copying from HPC3 to your data on the server.",
    "cleaning-up": "Queuing the removal of this session's files on HPC3.",
  })[st] || "";
}

function sessionDetailHtml(s, ttlDays) {
  const st = s.state || "open";
  const active = (s.active || []).map((t) => {
    const pct = t.percent != null ? t.percent : 0;
    return `<div class="sess-transfer"><div class="sess-transfer-text">${escapeHtml(transferText(t))}</div>` +
      (t.bytes_total ? `<div class="meter"><div class="meter-fill" style="width:${Math.max(0, Math.min(100, pct))}%"></div></div>` : "") +
      `</div>`;
  }).join("");
  const recentFails = (s.transfers || []).filter((t) => t.status === "failed" && t.kind === "stage-in").slice(-2)
    .map((t) => `<div class="sess-failure">${escapeHtml(transferText(t))}</div>`).join("");
  return `<div class="sess-row"><span class="sess-pill ${escapeHtml(st)}">${escapeHtml(SESSION_STATE_LABEL[st] || st)}</span>` +
    `<span class="sess-text">${escapeHtml(sessionHeadline(s, ttlDays))}</span></div>` + active + recentFails;
}

function renderSessionState(s) {
  state.session = s || null;
  const box = $("connSession"), chip = $("sessChip");
  if (!s) {
    if (box) { box.classList.add("hidden"); box.innerHTML = ""; }
    if (chip) chip.classList.add("hidden");
    return;
  }
  const st = s.state || "open";
  if (box) { box.innerHTML = sessionDetailHtml(s, state.ttlDays); box.classList.remove("hidden"); }
  if (chip) {
    // Idle and running are said elsewhere (the dot, the chat); the chip is for data on the move
    // and for what went wrong.
    const show = ["staging-in", "staging-out", "cleaning-up", "failed"].includes(st);
    chip.classList.toggle("hidden", !show);
    if (show) {
      const t = (s.active || [])[0];
      const pct = t && t.percent != null ? ` ${t.percent}%` : "";
      chip.className = `sess-chip ${st}`;
      // A phone shows the short word (styles.css), so the top bar still fits at 320 px.
      chip.innerHTML = `<span class="sc-long">${escapeHtml(SESSION_STATE_LABEL[st])}</span>` +
        `<span class="sc-short">${escapeHtml(SESSION_STATE_SHORT[st] || st)}</span>${escapeHtml(pct)}`;
      chip.title = sessionHeadline(s, state.ttlDays);
    }
  }
  if (storageShown()) renderComputeSessions(null, s);
}

let _computeSessionRows = [];
function renderComputeSessions(rows, live) {
  if (rows) _computeSessionRows = rows;
  if (live) _computeSessionRows = [live, ..._computeSessionRows.filter((r) => r.id !== live.id)];
  const el = $("computeSessionList"); if (!el) return;
  if (!_computeSessionRows.length) { el.innerHTML = '<div class="storage-empty">No sessions on HPC3 yet.</div>'; return; }
  el.innerHTML = _computeSessionRows.slice(0, 8).map((r) => {
    const when = r.closed_at ? `ended ${relTime(r.closed_at)}` : `opened ${relTime(r.opened_at)}`;
    const now = r.connection_id === state.connectionId && !r.closed_at ? " · this session" : "";
    return `<div class="storage-item sess-item"><div class="sess-item-main">${sessionDetailHtml(r, state.ttlDays)}` +
      `<div class="sess-when">${escapeHtml(`${r.hpc_user || ""} · ${when}${now}`)}</div></div></div>`;
  }).join("");
}

async function loadComputeSessions() {
  const el = $("computeSessionList"); if (!el) return;
  try {
    const q = state.connectionId ? `?connection_id=${encodeURIComponent(state.connectionId)}` : "";
    const res = await fetch(`/api/sessions${q}`);
    const d = await res.json();
    if (!res.ok) { el.innerHTML = `<div class="storage-empty">${escapeHtml(d.error || "Could not read the sessions")}</div>`; return; }
    if (d.ttl_days) state.ttlDays = d.ttl_days;
    renderComputeSessions(d.sessions || []);
  } catch (err) {
    el.innerHTML = `<div class="storage-empty">${isUnreachable(err) ? "Can't reach the local server." : escapeHtml(err.message)}</div>`;
  }
}

// ---- HPC3 originals of datasets now kept on the server (issue 6) ---------------------------------
// Two explicit steps, each a Slurm job on HPC3: a dry run that checks every original against its
// server copy, then, on the user's confirmation, the removal of what that check found.
let _originalsPoll = null;

function originalsPending(plan) {
  return !!plan && [plan.check, plan.remove].some((j) => j && j.status === "queued");
}

function renderOriginals(d) {
  const box = $("originalsBox"); if (!box) return;
  clearTimeout(_originalsPoll); _originalsPoll = null;
  // Shown only where originals can be checked at all (accounts, a data root, a real HPC3 session).
  const wrap = $("storeOriginals"); if (wrap) wrap.classList.toggle("hidden", !!(d && d.unavailable));
  if (d && d.unavailable) { box.innerHTML = `<div class="storage-empty">${escapeHtml(d.unavailable)}</div>`; return; }
  const plan = d && d.plan;
  const checkBtn = '<button type="button" class="ghost small" data-orig="check">Check what can be removed</button>';
  if (!plan) {
    box.innerHTML = `<p class="storage-note">When a dataset kept only on HPC3 is copied to the server, its HPC3 original is kept. ` +
      `A check runs as a job on HPC3 and removes nothing; you confirm the removal after it.</p>${checkBtn}`;
    return;
  }
  const parts = [];
  const check = plan.check, remove = plan.remove;
  if (!check && !(plan.candidates || []).length) {
    parts.push('<div class="storage-empty">No HPC3 original can be removed now.</div>');
  } else if (check && check.status === "queued") {
    parts.push(`<div class="orig-status">Checking ${plan.candidates.length} original(s) on HPC3 (job ${escapeHtml(check.job_id)}` +
      `${check.slurm_state ? `, ${escapeHtml(check.slurm_state.toLowerCase())}` : ", queued"}). Nothing is removed.</div>`);
  } else if (check && check.status === "failed") {
    parts.push(`<div class="sess-failure">The check did not finish: ${escapeHtml(check.error || "unknown error")}.</div>`);
  }
  const results = ((remove && remove.results) || (check && check.results) || []);
  if (results.length) {
    parts.push(results.map((r) => {
      const verdict = { would_remove: "can be removed", removed: "removed", skip: `kept: ${r.reason}` }[r.verdict] || r.verdict;
      return `<div class="storage-item orig-item ${escapeHtml(r.verdict)}"><span class="si-name">${escapeHtml(r.path)}</span>` +
        `<span class="si-size">${r.bytes != null ? escapeHtml(fmtSize(r.bytes)) + " · " : ""}${escapeHtml(verdict)}</span></div>`;
    }).join(""));
  }
  const kept = plan.kept || [];
  if (kept.length) {
    parts.push(`<details class="orig-kept"><summary>${kept.length} not considered</summary>` + kept.map((k) =>
      `<div class="storage-item"><span class="si-name">${escapeHtml(k.path || k.name)}</span><span class="si-size">${escapeHtml(k.reason)}</span></div>`
    ).join("") + "</details>");
  }
  if (remove && remove.status === "queued") {
    parts.push(`<div class="orig-status">Removing ${remove.entries} original(s) on HPC3 (job ${escapeHtml(remove.job_id)}` +
      `${remove.slurm_state ? `, ${escapeHtml(remove.slurm_state.toLowerCase())}` : ", queued"}).</div>`);
  } else if (remove && remove.status === "done") {
    parts.push(`<div class="orig-status done">Removed ${plan.removed} original(s), ${escapeHtml(fmtSize(plan.removed_bytes))}.</div>`);
  } else if (remove && remove.status === "failed") {
    parts.push(`<div class="sess-failure">The removal did not finish: ${escapeHtml(remove.error || "unknown error")}. Check again.</div>`);
  }
  const canRemove = check && check.status === "done" && !remove && plan.would_remove > 0;
  const buttons = [checkBtn];
  if (canRemove) buttons.unshift(`<button type="button" class="ghost small danger" data-orig="remove" data-plan="${escapeHtml(plan.id)}">` +
    `Remove ${plan.would_remove} original(s), ${escapeHtml(fmtSize(plan.would_remove_bytes))}</button>`);
  box.innerHTML = parts.join("") + `<div class="orig-actions">${buttons.join("")}</div>`;
  if (originalsPending(plan)) _originalsPoll = setTimeout(() => loadOriginals(plan.id), 8000);
}

async function loadOriginals(planId) {
  if (!state.connectionId) return;
  const q = `connection_id=${encodeURIComponent(state.connectionId)}` + (planId ? `&plan_id=${encodeURIComponent(planId)}` : "");
  try {
    const res = await fetch(`/api/hpc-originals?${q}`);
    const d = await res.json();
    if (!res.ok) { renderOriginals({ unavailable: d.error || "Not available." }); return; }
    if (storageShown()) renderOriginals(d);
  } catch (err) {
    renderOriginals({ unavailable: isUnreachable(err) ? "Can't reach the local server." : err.message });
  }
}

async function originalsAction(kind, planId) {
  if (kind === "remove" && !confirm("Remove these HPC3 originals?\n\nEach one is checked again on HPC3 " +
      "against its copy on the server before it is removed. This cannot be undone.")) return;
  const url = kind === "remove" ? "/api/hpc-originals/remove" : "/api/hpc-originals/check";
  try {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, plan_id: planId || "" }) });
    const d = await res.json();
    if (!res.ok) { toast(d.error || "Failed"); if (d.plan) renderOriginals(d); return; }
    renderOriginals(d);
  } catch (err) { toast(isUnreachable(err) ? "Can't reach the local server." : err.message); }
}

// ---- My account → Storage (RCHENLAB/AiScientist issue 32) ---------------------------------------
// Where the account's files are. On the server: its datasets and its runs' folders, kept until
// deleted; read without an HPC3 session. On HPC3: the account's own folders there, read over this
// session's SSH, area by area, each saying what happens to the files in it.
let _storeServer = null;                                   // the server listing as last read
const _storeShowAll = { datasets: false, outputs: false };
const STORE_ROWS = 6;                                      // rows a list shows before "Show all"

// True while My account → Storage is on screen: polls and redraws stop when it is not.
function storageShown() {
  const view = $("view-account"), pane = $("accountPane-storage");
  return !!view && view.classList.contains("active") && !!pane && !pane.classList.contains("hidden");
}

async function loadStorageTab() {
  loadServerStorage();
  loadComputeSessions();
  if (!state.connectionId) { renderStorage(null); return; }
  $("storeHpcBody").innerHTML = '<div class="storage-loading">Loading…</div>';
  loadOriginals();
  loadStorageDeletes();
  await loadStorageListing();
}

// The session chip in the header: what the session is doing with HPC3, in detail.
function openStorageSessions() {
  openAccount("storage");
  const d = $("storeSessions");
  if (d) d.open = true;
}

function fmtEpochDay(t) {
  try { return new Date(t * 1000).toLocaleDateString("en-US", { month: "short", day: "numeric" }); } catch { return ""; }
}

// One row of either section: the name (and a line under it), its size, then its action.
function storeRowHtml({ name, meta, bytes, title, action }) {
  return `<div class="store-row"${title ? ` title="${escapeHtml(title)}"` : ""}><div class="store-row-main">` +
    `<span class="store-name">${escapeHtml(name)}</span>${meta ? `<span class="store-meta">${escapeHtml(meta)}</span>` : ""}</div>` +
    `<span class="store-size">${bytes == null ? "" : escapeHtml(fmtSize(bytes))}</span>${action || ""}</div>`;
}

// What share of the section each kind takes, as one bar and its legend.
function storeBarHtml(parts) {
  const total = parts.reduce((n, p) => n + (p.bytes || 0), 0);
  const bar = total ? `<div class="store-bar" aria-hidden="true">` + parts.filter((p) => p.bytes > 0)
    .map((p) => `<i class="${p.cls}" style="flex-grow:${p.bytes}"></i>`).join("") + `</div>` : "";
  return bar + `<div class="store-legend">` + parts.map((p) =>
    `<span><i class="store-dot ${p.cls}"></i>${escapeHtml(p.label)} <b>${escapeHtml(fmtSize(p.bytes || 0))}</b></span>`).join("") + `</div>`;
}

function storeListHtml(key, title, rows, empty) {
  const all = _storeShowAll[key];
  const more = rows.length > STORE_ROWS
    ? `<button type="button" class="ghost small store-more" data-store-more="${key}">${all ? "Show fewer" : `Show all ${rows.length}`}</button>` : "";
  return `<div class="store-group"><div class="store-group-head"><strong>${escapeHtml(title)}</strong>` +
    `<span class="store-count">${rows.length}</span></div>` +
    (rows.length ? (all ? rows : rows.slice(0, STORE_ROWS)).join("") + more : `<div class="storage-empty">${escapeHtml(empty)}</div>`) + `</div>`;
}

async function loadServerStorage() {
  const body = $("storeServerBody");
  try {
    const res = await fetch("/api/storage/server");
    const d = await res.json();
    if (!res.ok) {
      $("storeServerTotal").textContent = "";
      body.innerHTML = `<div class="storage-empty">${escapeHtml(d.error || "Could not read your storage on the server.")}</div>`;
      return;
    }
    const results = await loadResultHandoffs(false).catch(() => ({}));
    renderServerStorage(d, results || {});
  } catch (err) {
    body.innerHTML = `<div class="storage-empty">${isUnreachable(err) ? "Can't reach the server." : escapeHtml(err.message)}</div>`;
  }
}

function renderServerStorage(d, results) {
  if (d) _storeServer = { d, results: results || {} };
  if (!_storeServer) return;
  ({ d, results } = _storeServer);
  const datasets = d.datasets || [], outputs = d.outputs || [];
  $("storeServerTotal").textContent = fmtSize(d.total_bytes || 0);
  const dsRows = datasets.map((x) => storeRowHtml({
    name: x.name, bytes: x.bytes, title: x.path || "",
    meta: [x.kind, x.uploaded_at ? `added ${fmtDay(x.uploaded_at)}` : ""].filter(Boolean).join(" · "),
    action: `<button type="button" class="ghost small danger" data-del="${x.id}" data-name="${escapeHtml(x.name)}" ` +
            `data-kind="${escapeHtml(x.kind || "")}" data-location="server" data-path="${escapeHtml(x.path || "")}">Delete</button>`,
  }));
  const runRows = outputs.map((r) => storeRowHtml({
    name: runTitle(r), bytes: r.bytes, title: `Run ${r.run_id}`,
    meta: [r.status, r.created_at ? fmtDay(r.created_at) : ""].filter(Boolean).join(" · "),
    action: r.running ? '<span class="store-meta">running</span>'
      : `<button type="button" class="ghost small danger" data-run-del="${escapeHtml(r.run_id)}" data-name="${escapeHtml(runTitle(r))}" ` +
        `data-kept="${keptBesideRun(results[r.run_id]) ? "1" : ""}">Delete</button>`,
  }));
  const notes = [];
  if (d.linked) notes.push(`${d.linked} of your datasets only point at a shared folder on the server and take none of your space.`);
  if (d.on_hpc3) notes.push(`${d.on_hpc3} of your datasets are still kept on HPC3. They are copied to the server when you next connect.`);
  $("storeServerBody").innerHTML =
    storeBarHtml([{ label: "Datasets", bytes: d.datasets_bytes, cls: "k-datasets" },
                  { label: "Run outputs", bytes: d.outputs_bytes, cls: "k-outputs" }]) +
    storeListHtml("datasets", "Datasets", dsRows, "No datasets yet.") +
    storeListHtml("outputs", "Run outputs", runRows, "No run has written anything yet.") +
    notes.map((n) => `<p class="store-note">${escapeHtml(n)}</p>`).join("");
}

async function onServerStorageClick(e) {
  const more = e.target.closest("[data-store-more]");
  if (more) { _storeShowAll[more.dataset.storeMore] = !_storeShowAll[more.dataset.storeMore]; renderServerStorage(); return; }
  const ds = e.target.closest("button[data-del]");
  if (ds) { if (await deleteDatasetRow(ds)) loadServerStorage(); return; }
  const run = e.target.closest("button[data-run-del]");
  if (run && await deleteRun(run)) loadServerStorage();
}

async function loadStorageListing() {
  try {
    const res = await fetch(`/api/storage/${state.connectionId}`);
    const d = await res.json();
    if (!res.ok) { $("storeHpcBody").innerHTML = `<div class="storage-empty">${escapeHtml(d.error || "Could not read your folders on HPC3.")}</div>`; return; }
    renderStorage(d);
  } catch (err) {
    $("storeHpcBody").innerHTML = `<div class="storage-empty">${isUnreachable(err) ? "Can't reach the server." : escapeHtml(err.message)}</div>`;
  }
}

// The lab's quota is a GROUP quota: RCIC reports no per-member figure.
function quotaHtml(q) {
  const pct = q.percent == null ? null : Math.min(100, Number(q.percent));
  const tone = pct >= 95 ? " tone-over" : pct >= 85 ? " tone-warn" : "";
  return `<div class="store-quota"><div class="usage-bar-legend"><span>Lab quota on ${escapeHtml(q.pool)}, shared by everyone in ${escapeHtml(q.group)}</span>` +
    `<span>${escapeHtml(q.used)} of ${escapeHtml(q.limit)}${pct == null ? "" : ` · ${pct}%`}</span></div>` +
    (pct == null ? "" : `<div class="usage-bar${tone}"><i style="width:${pct}%"></i></div>`) + `</div>`;
}

function storageItemHtml(i) {
  const deleting = i.deleting || _storageDeleting[i.path];
  const meta = [];
  if (i.changed) meta.push(`last used ${fmtEpochDay(i.changed)}`);
  if (i.clean_after) meta.push(i.clean_after * 1000 <= Date.now() ? "removed at the next cleanup" : `removed after ${fmtEpochDay(i.clean_after)}`);
  const action = deleting
    ? `<span class="si-deleting">Deletion queued (job ${escapeHtml(deleting)})</span>`
    : `<button type="button" class="ghost small danger" data-path="${escapeHtml(i.path)}">Delete</button>`;
  return storeRowHtml({ name: i.name, meta: meta.join(" · "), bytes: i.bytes, title: i.path, action });
}

// ``d`` = GET /api/storage/<connection>; null = no HPC3 session to read the folders with.
function renderStorage(d) {
  _storageListing = d;
  const body = $("storeHpcBody"); if (!body) return;
  if (!d) {
    $("storeHpcTotal").textContent = "";
    $("storeOriginals").classList.add("hidden");
    body.innerHTML = `<div class="storage-empty">Connect to HPC3 on the Research page to see your folders there. ` +
      `Temporary run files are removed when a session ends${state.ttlDays ? `, or ${state.ttlDays} days after they were last used` : ""}.</div>`;
    return;
  }
  const areas = d.areas || [];
  $("storeHpcTotal").textContent = fmtSize(areas.reduce((n, a) => n + (a.bytes || 0), 0));
  const parts = d.quota ? [quotaHtml(d.quota)] : [];
  for (const a of areas) {
    const items = a.items || [];
    if (a.key !== "temp" && !items.length) continue;     // the older areas show only while they hold something
    const rows = items.map(storageItemHtml).join("") || '<div class="storage-empty">Nothing here now.</div>';
    const size = `<span class="store-size">${escapeHtml(fmtSize(a.bytes || 0))}</span>`;
    const note = `<p class="store-note">${escapeHtml(a.note || "")}</p>`;
    // The member's own lab folder holds their own files too: folded until they open it.
    parts.push(a.key === "personal"
      ? `<details class="store-group store-fold"><summary class="store-group-head"><strong>${escapeHtml(a.label)}</strong>` +
        `<span class="store-count">${items.length}</span>${size}</summary>${note}${rows}</details>`
      : `<div class="store-group"><div class="store-group-head"><strong>${escapeHtml(a.label)}</strong>${size}</div>${note}${rows}</div>`);
  }
  body.innerHTML = parts.join("");
}

// ---- a Delete on HPC3 is a Slurm job ------------------------------------------------------------
// The server answers at once with the queued job; the Storage tab asks for its state (one sacct)
// while it is queued or running, and reads the folders again once it has ended.
let _storageDeletesPoll = null;
let _storageDeleting = {};        // path -> job id, for the deletions still queued or running
let _storageListing = null;       // the folders as last read, to redraw a row as queued

function storageDeleteLine(j) {
  const where = `<span class="si-name">${escapeHtml(j.path)}</span>`;
  if (j.status === "queued") {
    return `<div class="storage-item sd-item queued">${where}<span class="si-size">deleting, job ${escapeHtml(j.job_id)}` +
      `${j.slurm_state ? ` ${escapeHtml(j.slurm_state.toLowerCase())}` : " queued"}</span></div>`;
  }
  if (j.status === "done") {
    return `<div class="storage-item sd-item done">${where}<span class="si-size">deleted` +
      `${j.note ? ` (${escapeHtml(j.note)})` : ""}</span></div>`;
  }
  return `<div class="storage-item sd-item failed">${where}<span class="si-size sess-failure">not deleted: ` +
    `${escapeHtml(j.error || "unknown error")}</span></div>`;
}

function renderStorageDeletes(deletes) {
  const box = $("storageDeletes"); if (!box) return [];
  clearTimeout(_storageDeletesPoll); _storageDeletesPoll = null;
  const shown = (deletes || []).filter((j) => j.job_id !== "mock").slice(-5).reverse();
  box.innerHTML = shown.length ? `<div class="sd-head">Deletions on HPC3</div>` + shown.map(storageDeleteLine).join("") : "";
  const before = _storageDeleting;
  _storageDeleting = {};
  for (const j of deletes || []) if (j.status === "queued") _storageDeleting[j.path] = j.job_id;
  const ended = Object.keys(before).filter((path) => !(path in _storageDeleting));
  if (Object.keys(_storageDeleting).length) _storageDeletesPoll = setTimeout(loadStorageDeletes, 8000);
  return ended;
}

async function loadStorageDeletes() {
  if (!state.connectionId) return;
  try {
    const res = await fetch(`/api/storage/${state.connectionId}/deletes`);
    const d = await res.json();
    if (!res.ok) {
      if (Object.keys(_storageDeleting).length) { clearTimeout(_storageDeletesPoll); _storageDeletesPoll = setTimeout(loadStorageDeletes, 15000); }
      return;
    }
    if (!storageShown()) { clearTimeout(_storageDeletesPoll); _storageDeletesPoll = null; return; }
    const ended = renderStorageDeletes(d.deletes);
    if (ended.length) loadStorageListing();          // a deletion ended: the folders changed
  } catch (err) { /* the next open of the panel asks again */ }
}

async function deleteStorageItem(path) {
  if (!confirm(`Delete on HPC3:\n\n${path}\n\nThe deletion runs as a job on HPC3, inside your own folders only. ` +
      `This cannot be undone. Continue?`)) return;
  try {
    const res = await fetch("/api/storage/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connection_id: state.connectionId, path }) });
    const d = await res.json();
    if (!res.ok) { toast(d.error || "Delete failed"); return; }
    if (d.status === "deleted") { toast("Deleted."); renderStorage(d); return; }
    toast(`Deletion queued on HPC3 (job ${d.job.job_id}). The list updates when it has run.`);
    renderStorageDeletes(d.deletes);
    if (_storageListing) renderStorage(_storageListing);      // the row now says it is queued
  } catch (err) { toast(isUnreachable(err) ? "Can't reach the local server." : "Delete failed: " + err.message); }
}

async function stopGpu() {
  if (!state.connectionId) return;
  if (!confirm("Stop your own GPU job (scancel)? It frees the GPU and stops your SU charge. Only your own job is stopped; no one else is affected.")) return;
  await doStopGpu();
}

// Drop a stored connection the server refuses to serve to this user, without calling it.
function forgetConnection() {
  if (state.ws) { try { state.ws.close(); } catch {} }
  try { localStorage.removeItem(CONN_KEY); localStorage.removeItem(RUNOWNER_KEY); localStorage.removeItem(PLANDRAFT_KEY); } catch {}
  state.connectionId = null;
  state.lastJobId = null;
  applyStatus({ status: "disconnected", model: "" });
}

async function disconnect() {
  if (!state.connectionId) { applyStatus({ status: "disconnected", model: "" }); return; }
  if (state.lastJobId && state.status === "ready") {
    if (confirm(`GPU job ${state.lastJobId} is still running and keeps charging SU.\n\nOK = stop it too (scancel, frees your SU)\nCancel = just disconnect, leave it running`)) {
      await doStopGpu();
    }
  }
  try {
    const res = await fetch("/api/disconnect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connection_id: state.connectionId }) });
    const d = await res.json().catch(() => ({}));
    // How the session's HPC3 files went: its cleanup job, or why they are left to the sweep.
    if (res.ok && d.session && d.session.closed_at) {
      const said = sessionHeadline(d.session, state.ttlDays);
      if (said) toast(`Session closed. ${said}`);
    } else if (res.ok && d.ending) {
      // Large results take minutes to copy; the server goes on after this reply.
      toast("Session closed. Its results are being copied from HPC3 to the server; its HPC3 files are removed once they are checked. My account → Storage → Sessions shows how it ends.");
    }
  } catch (err) {
    if (isUnreachable(err)) toast("Local server was already down — clearing the session. Check HPC3 with `squeue --me`.");
  }
  if (state.ws) { try { state.ws.close(); } catch {} }
  try { localStorage.removeItem(CONN_KEY); } catch {}
  state.connectionId = null;
  state.lastJobId = null;
  applyStatus({ status: "disconnected", model: "" });
}

function setChatRunning(c, running) {
  c.running = running;
  // Any legit terminal event (chat_done / chat_error / run_complete) clears `running` and so cancels
  // the silent-death watchdog — only a run that ended with NO terminal event lets the timer fire.
  if (!running && c.deadRunTimer) { clearTimeout(c.deadRunTimer); c.deadRunTimer = null; }
  if (chatVisible(c)) {
    const stop = $("chatStop");
    if (stop) { stop.disabled = false; stop.textContent = "■ Stop"; }
  }
  saveRunOwners();
  updateComposerButton();
  renderSessionList();
}
function setRunning(running) { const c = activeChat(); if (c) setChatRunning(c, running); }

// The composer has ONE primary action that swaps between Send and Stop, for the chat on screen:
//  • idle (no run, no plan)              → Send  (starts a new run)
//  • plan review / executing + has text  → Send  (refine the plan, or inject mid-run)
//  • plan review / executing + empty box → Stop  (cancel the pending plan / the run)
// So the moment the user types during a plan review OR a running analysis, Stop becomes
// Send and they can submit — without the giant Stop button hiding the input. Another chat's run
// changes nothing here: this chat can start its own.
function updateComposerButton() {
  const send = $("chatSend"), stop = $("chatStop"), input = $("chatInput");
  if (!send || !stop || !input) return;
  const hasText = input.value.trim().length > 0;
  const c = activeChat();
  const busy = !!c && (c.planPending || c.running);
  const showSend = !busy || hasText;             // idle → Send; busy → Send only once typing
  send.classList.toggle("hidden", !showSend);
  stop.classList.toggle("hidden", showSend);
  renderServerPathOffer();   // a server path in the text → offer to bind it; gone once the box clears
}

// Cancel a chat's in-flight run so the GPU + compute are released. Used by the Stop
// button and by deleting the chat that owns the run.
async function stopRun(c = activeChat()) {
  if (!state.connectionId || !c) return;
  if (chatVisible(c)) {
    const stop = $("chatStop");
    if (stop) { stop.disabled = true; stop.textContent = "■ Stopping…"; }
  }
  try {
    await fetch("/api/chat/stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      // Stop only THIS chat's run — never another chat's run on the shared session.
      body: JSON.stringify({ connection_id: state.connectionId, conversation_id: c.sid }),
    });
  } catch { /* the chat_stopped event (or next render) will settle the UI */ }
}

// Steer a chat's executing run without stopping it — the note is folded into the remaining
// steps by the lab loop (see /api/chat/inject).
async function injectPrompt(c, text) {
  try {
    const res = await fetch("/api/chat/inject", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, conversation_id: c.sid, text }),
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) { toast(d.error || "Couldn't add your note to the run."); return; }
    toast("Added to the run — applies from the next step.");
  } catch (err) { toast(isUnreachable(err) ? "Can't reach the local server." : "Inject failed: " + err.message); }
}

async function sendChat(e) {
  e.preventDefault();
  const input = $("chatInput");
  const text = input.value.trim();
  if (!text) return;
  // Everything below is about THIS chat: its plan, its run, its attachments. A message typed in a
  // chat is never feedback on another chat's plan, never a note for another chat's run — a tester
  // opened a fresh chat with "+", typed "Analyze this dataset", and it used to be sent to the PI as
  // a change request for the previous chat's plan.
  const c = activeChat();
  if (!c) return;
  if (c.datasetSave && !await c.datasetSave) return;
  // While a PI plan/clarify is awaiting a decision, the composer is the "refine the plan" channel —
  // the message is natural-language feedback that re-plans (it does NOT start a new run).
  if (c.planPending) {
    input.value = "";
    c.draft = "";
    saveComposer(c);
    updateComposerButton();
    revisePlanInWords(c, text);
    return;
  }
  // A run is already executing in this chat (post-plan): the message is mid-run steering — queue it
  // for the lab loop to fold into the remaining steps, rather than launching a 2nd run.
  if (c.running) {
    appendUserMessage(text);
    input.value = "";
    c.draft = "";
    saveComposer(c);
    updateComposerButton();
    injectPrompt(c, text);
    return;
  }
  // A run needs a served model, and the session only has one at "ready" — mirrors the /api/lab guard.
  if (state.status !== "ready") { toast("Connect to HPC3 first."); return; }
  // The session's limit on runs at once, checked here before the question is saved to the chat: the
  // server refuses it anyway (and has the last word — runs started from another window count too).
  const inFlight = [...chats.values()].filter((x) => x.running || (x.planPending && !(x.plan && x.plan.saved))).length;
  if (state.maxRuns && inFlight >= state.maxRuns) {
    toast(`${inFlight} chats already have a run in progress on this session (at most ${state.maxRuns} at once). Wait for one to finish, or stop one, then send this again.`);
    return;
  }
  appendUserMessage(text);
  // This chat owns its run, so its streamed report and artifacts land here even if the user
  // switches chats while it runs (pipelines take minutes). Persisted too (saveRunOwners), so a
  // reload/reconnect (which replays the live run) re-binds it to this chat instead of falling back
  // to whatever chat is active — the cross-conversation leak.
  c.owned = true;
  input.value = "";
  c.draft = "";
  saveComposer(c);
  setChatRunning(c, true);
  const sess = activeSession();
  // An approved plan is part of the conversation a model is given: its steps and its document.
  const history = sess.messages.filter((m) => (!m.kind || m.kind === "plan") && (m.role === "user" || m.role === "assistant")).slice(0, -1).map((m) => ({ role: m.role, content: m.content }));
  const dataset_path = c.primary || null;
  // The BIND-SET (feature ②): every data file attached to THIS chat, ordered as attached (the server
  // re-ranks to pick the primary). Legacy single-file runs still send `dataset_path`; both together
  // stay backward-compatible (the server treats `dataset_path` as the primary when `datasets` is absent).
  const datasets = c.bound.map((p) => {
    const d = state.datasets.find((x) => x.path === p) || {};
    return { path: p, name: d.name || p.split("/").pop(), role: c.roles[p] || null };
  });
  // DAG is the default execution model now (dependency-graph + Coordinator; no UI toggle). The
  // classic linear order is still reachable server-side via AISCIENTIST_PLANNER=linear.
  const planner = "dag";
  // A refused send leaves the chat idle and gives the text back, so it can be sent again.
  const giveBack = () => {
    setChatRunning(c, false);
    if (chatVisible(c) && !input.value.trim()) { input.value = text; c.draft = text; updateComposerButton(); }
    else if (!c.draft) c.draft = text;
    saveComposer(c);
  };
  try {
    // One flow for every request, with the same run options (FIXED_RUN_OPTIONS): the server routes
    // it to a literature search or to the lab, and either starts with a plan to review. `history` is
    // the conversation so far, for a line that reads it.
    const labBody = { connection_id: state.connectionId, conversation_id: c.sid, question: text, dataset_path, datasets, ...FIXED_RUN_OPTIONS, planner, history, tz: userTz() };
    const postLab = () => fetch("/api/lab", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(labBody) });
    let res = await postLab();
    // The budget hard limit (Account › API usage): "confirm" asks once, then sends the same run again.
    if (res.status === 409) {
      const d = await res.clone().json().catch(() => ({}));
      if (d.needs_budget_confirm) {
        if (!confirm(`${d.error}\n\nThe figure is an estimate; the Claude Console has the actual spend.`)) { giveBack(); return; }
        labBody.budget_ack = true;
        res = await postLab();
      }
    }
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      if (res.status === 404 && /unknown connection/i.test(d.error || "")) {
        // The server no longer knows this connection: it restarted under us. A bare "Unknown
        // connection id" toast read as "the question did nothing" — say what happened and what to
        // do, and drop the dead connection so the connect button comes back.
        toast("The server restarted and this session ended — reconnect to HPC3 to continue (your GPU job may still be running and will reattach).");
        state.connectionId = null;
        setChatRunning(c, false);
        try { showReattachHint(); } catch { /* banner is best-effort */ }
        try { renderConnectState && renderConnectState(); } catch { /* optional */ }
        return;
      }
      giveBack();
      toast(d.error || "Run failed");
    }
  } catch (err) {
    giveBack();
    toast(isUnreachable(err) ? "Can't reach the local server." : "Run failed: " + err.message);
  }
}

// ---- plan-mode review: the plan/clarify render AS chat messages (plan.md style) -------
// The PI proposes a plan rendered into the conversation like a plan.md, with inline
// Run / Cancel actions. The user refines it by typing in the composer (which re-plans);
// an ambiguous request comes back as a clarify card with option chips. The live card is
// ephemeral (buttons can't persist); on approval the plan is saved to history as text.
const DEFAULT_PLACEHOLDER = "Ask a question, or describe the analysis you want — the agent proposes a plan first…";

// ---- the prompt that is waiting, kept apart from its card ---------------------------------------
// A plan, a clarify question or a decision point waits for the user in ONE chat: the one that owns
// the run. Several chats may each have one waiting. Its card is DOM, and renderChat() empties the
// stream on every switch of chats, so the card alone cannot be where the prompt lives: switching
// away and back left a composer that said a plan was pending and no Run / Cancel / Edit as YAML to
// act on, until a full page reload made the server replay it. So the prompt is held in its chat's
// record (``c.plan``) from the moment it arrives until it is resolved, and the card is drawn FROM
// it: when it arrives and its chat is on screen, when that chat is rebuilt, and for the first time
// when it arrived while another chat was shown.

function planPromptVisible(c = activeChat()) {
  return !!c && !!c.plan && chatVisible(c);
}

function holdPlanPrompt(c, kind, payload, msg) {
  const held = c.plan;
  const next = { kind, sid: c.sid, run: (msg && msg.run_id) || null, ...payload };
  if (msg && msg.saved) next.saved = true;
  if (kind === "plan") {
    // The server sends a pending plan again whenever a socket subscribes (a reconnect, a reload),
    // without the `revised` marks: a plan that is repeated keeps the ones it first came with.
    const repeated = !!held && held.kind === "plan"
      && JSON.stringify([held.agenda, held.doc]) === JSON.stringify([next.agenda, next.doc]);
    next.revised = msg.revised || (repeated ? held.revised : []);
  }
  const same = !!held && JSON.stringify(held) === JSON.stringify(next);
  c.plan = next;
  c.planPending = true;
  c.answered = null;
  // Review time must not appear as compute time, including on replayed prompts.
  if (c.run) c.run.startedAt = null;
  if (chatVisible(c)) repaintWorking();
  if (kind === "plan") keepPlanDraft(c, next);
  saveRunOwners();
  // The prompt that is already on screen, sent again: its card stays as it is, with the caret
  // where the person left it.
  if (same && chatVisible(c) && state.planCardEl && state.planCardEl.isConnected) return;
  renderSessionList();
  if (chatVisible(c)) {
    state.planView = null;
    drawPlanPrompt(c);
    return;
  }
  // Another chat is on screen. The prompt is not drawn into it (it would be a card for a plan that
  // chat never asked for, and the owning chat would then show none); it waits for its own chat.
  updateComposerButton();
  if (!same) toast(`“${chatTitle(c.sid).trim()}” is waiting for your answer — open that chat to reply.`);
}

function drawPlanPrompt(c = activeChat()) {
  const p = c && c.plan;
  if (!c || !chatVisible(c)) return;
  if (!p) { drawAnsweredPlan(c); return; }
  if (p.kind === "clarify") showClarify(p.questions);
  else if (p.kind === "decision") showDecision(p.goal, p.options);
  else showPlanPanel(p.agenda);
}

// The prompt was answered, or can no longer be: nothing is drawn for it again.
function releasePlanPrompt(c = activeChat()) {
  if (!c) return;
  const was = !!c.plan || c.planPending;
  c.plan = null;
  c.planPending = false;
  saveRunOwners();
  if (was) renderSessionList();
}

// What is typed in a plan editor and not applied, and whether the editor is open, belong to ONE plan
// document (``c.planDraftBase``) of one chat. They are kept for as long as the server keeps sending
// that document: when the card is rebuilt, when the prompt is replayed after a reconnect or a page
// reload (the browser's storage carries them over the reload), when the PI answers a question and
// leaves the plan alone. A different document drops them. An edit the server refused left the plan
// unchanged, so what was typed stays.
function keepPlanDraft(c, p) {
  const yaml = (p.doc && p.doc.yaml) || "";
  if (yaml && !c.planDraftBase) loadPlanDraft(c, p);
  const refused = (p.problems || []).length > 0 && !!c.planDraftYaml;
  if (!yaml || (c.planDraftBase !== yaml && !refused)) {
    c.planDraftYaml = "";
    c.planEditorOpen = false;
  }
  c.planDraftBase = yaml;
  savePlanDraft(c);
}

// The editor reports each change as it happens, so there is never a card whose text exists only in
// its textarea. Returns whether the text differs from the plan it edits.
function notePlanDraft(text, c = activeChat()) {
  if (!c) return false;
  const original = (c.plan && c.plan.doc && c.plan.doc.yaml) || "";
  const dirty = !!text.trim() && text.trim() !== original.trim();
  c.planDraftYaml = dirty ? text : "";
  c.planDraftBase = original;
  savePlanDraft(c);
  return dirty;
}

function notePlanEditorOpen(open, c = activeChat()) {
  if (!c) return;
  c.planEditorOpen = !!open;
  savePlanDraft(c);
}

// Every chat's draft, in one storage entry keyed by chat. An older page stored a single
// {sid, run, base, text, open}; it is read as that one chat's.
function readPlanDrafts() {
  let d = null;
  try { d = JSON.parse(localStorage.getItem(PLANDRAFT_KEY) || "null"); } catch {}
  if (!d || typeof d !== "object") return {};
  if (typeof d.sid === "string") return { [d.sid]: d };
  return d;
}

function writePlanDrafts(all) {
  try {
    if (Object.keys(all).length) localStorage.setItem(PLANDRAFT_KEY, JSON.stringify(all));
    else localStorage.removeItem(PLANDRAFT_KEY);
  } catch { /* private mode: the draft still survives a re-render, only not a reload */ }
}

function savePlanDraft(c) {
  const p = c.plan;
  const all = readPlanDrafts();
  if (p && p.kind === "plan" && c.planDraftBase && (c.planDraftYaml || c.planEditorOpen)) {
    all[c.sid] = { run: p.run, base: c.planDraftBase, text: c.planDraftYaml, open: c.planEditorOpen };
  } else {
    delete all[c.sid];
  }
  writePlanDrafts(all);
}

// After a page reload: what this browser had typed for THIS run's plan. keepPlanDraft then checks
// it against the document the server sent.
function loadPlanDraft(c, p) {
  const d = readPlanDrafts()[c.sid];
  if (!d || (d.run || null) !== p.run) return;
  c.planDraftBase = String(d.base || "");
  c.planDraftYaml = String(d.text || "");
  c.planEditorOpen = !!d.open;
}

function dropPlanDraft(c = activeChat()) {
  if (!c) return;
  c.planDraftYaml = "";
  c.planDraftBase = "";
  c.planEditorOpen = false;
  const all = readPlanDrafts();
  if (c.sid in all) { delete all[c.sid]; writePlanDrafts(all); }
}

function removePlanCard() {
  if (state.planCardEl) { state.planCardEl.remove(); state.planCardEl = null; }
  state.planStatusEl = null;   // the old card's status line went with it
}

// kind: "plan" | "clarify" | "decision" — what the card waits for.
function planCardShell(kind) {
  removePlanCard();
  if (state.planAnsweredEl) { state.planAnsweredEl.remove(); state.planAnsweredEl = null; }
  const stream = $("chatStream");
  const hint = stream.querySelector(".empty-hint"); if (hint) hint.remove();
  const el = document.createElement("div");
  el.className = "msg assistant plan-card";
  el.dataset.prompt = kind;
  stream.appendChild(el);
  state.planCardEl = el;
  return el;
}


// A step arrives as `**Short title** — detailed prose`; it is shown as a title with the prose under
// it, so the plan scans as a pipeline (titles) with detail on demand.
//
// Two shapes had to be tolerated rather than assumed. A step may carry its own ordinal
// ("1. **QC & normalization** — …") when a model echoed the numbered plan back on the revision
// path; read with `**` anchored to the very start, that step fell through to the untitled branch
// and rendered as a BARE "1" heading followed by raw markdown — the reviewer's only visible sign
// that a step had changed was that it looked broken (Ziyao, plan_mode_report_v2_5, 6/6 on that
// path). The server now strips the ordinal, and this tolerates it anyway so an older server or a
// fallback plan can't reproduce the bug. And a genuinely untitled step gets its FIRST CLAUSE as
// the title instead of nothing, so no step is ever a number on its own.
function planStepParts(text) {
  const body = String(text == null ? "" : text).replace(/^\(?\d{1,2}[.)、]\s+/, "").trim();
  const m = /^\*\*(.+?)\*\*\s*[—–:-]+\s*([\s\S]*)$/.exec(body);
  if (m) return { title: m[1], body: m[2] };
  const lead = /^(.{4,70}?)(?:\s+[—–-]\s+|[.:]\s+)([\s\S]*)$/.exec(body);
  return lead ? { title: lead[1], body: lead[2] } : { title: body || "(empty step)", body: "" };
}

function planSettingText(value) {
  if (Array.isArray(value)) return value.map(planSettingText).join(", ");
  if (value === null || value === undefined) return "none";
  return String(value);
}

function planRunsOnText(where) {
  const w = String(where || "");
  if (w.startsWith("gpu:")) return "GPU job on HPC3";
  if (w.startsWith("hpc:")) return "Slurm job on HPC3";
  if (w === "inprocess") return "Runs on the AiScientist server";
  return "";
}

function planIcon(name) {
  return `<span class="material-symbols-outlined msym" aria-hidden="true">${name}</span>`;
}

// The numbers in a step's prose are what a reviewer checks (a cut-off, a threshold, a count), so
// they are set in bold, and a value the plan passes to the tool (one of its settings) is marked as
// that setting. Runs over the HTML renderMarkdown made, on its text only: never inside a tag, an
// entity (`&#039;`), a code span or a link. A digit inside a word (DDX41, log1p) is not a number.
function planEmphasisHtml(html, settings) {
  const s = settings || {};
  const words = new Map();     // a text setting as it appears in the escaped prose -> its name
  for (const [k, v] of Object.entries(s)) {
    for (const x of Array.isArray(v) ? v : [v]) {
      if (typeof x === "string" && x.trim().length > 1) words.set(escapeHtml(x.trim()), k);
    }
  }
  const alt = [...words.keys()].sort((a, b) => b.length - a.length)
    .map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
  const re = new RegExp("(^|[^\\w.#&-])(?:([~≈]?\\d+(?:[.,]\\d+)*(?:%|x(?![a-z])|×|-fold)?)"
    + (alt ? `|(${alt})` : "") + ")(?!\\w)", "g");
  const pinOf = (num) => {
    const n = parseFloat(num.replace(/[~≈,]/g, ""));
    const pct = num.endsWith("%");
    for (const [k, v] of Object.entries(s)) {
      for (const x of Array.isArray(v) ? v : [v]) {
        if (typeof x === "number" && (Math.abs(x - n) < 1e-9 || (pct && Math.abs(x - n / 100) < 1e-9))) return k;
      }
    }
    return "";
  };
  let skip = 0;
  return String(html).split(/(<[^>]*>)/).map((part) => {
    if (part.startsWith("<")) {
      if (/^<(code|a|pre)[\s>]/i.test(part)) skip += 1;
      else if (/^<\/(code|a|pre)>/i.test(part)) skip = Math.max(0, skip - 1);
      return part;
    }
    if (skip) return part;
    return part.replace(re, (_m, pre, num, word) => {
      const key = num ? pinOf(num) : words.get(word);
      return pre + (key
        ? `<strong class="plan-pin" title="Passed to the tool as ${escapeHtml(key)}">${num || word}</strong>`
        : `<strong class="plan-num">${num}</strong>`);
    });
  }).join("");
}

// Shown in full or as its first two lines: as the person left it, else in full only when the last
// revision rewrote it (what changed is what they need to read).
function planStepOpen(text, revised) {
  const kept = state.planStepOpen;
  return kept && kept.has(text) ? kept.get(text) : !!revised;
}

// One step of the card. `step` is its entry in the plan document (tool, settings, where it runs)
// when the server sent one; without it the step is its title and prose alone. The title, the tool
// and the settings are always shown; the prose is two lines until the step is opened.
// `edit`: the plan is waiting and has a document, so its title and setting values can be changed
// in place and (`edit.remove`) the step removed (wirePlanCardEdits).
// A step whose tool runs code the AI writes during the run (agents/plan_doc.py CODE_TOOLS) is not a
// tested AiScientist tool: the plan says so before it is approved (issue 13). A vetted skill on
// the step does not change that: the AI adapts the skill's code to the data during the run.
const PLAN_CODE_TOOLS = new Set(["run_code", "run_shell", "run_in_environment"]);
function planUntestedFlag(step) {
  if (!step || !PLAN_CODE_TOOLS.has(step.tool)) return "";
  const why = step.skill
    ? `No tested tool does this step: the AI adapts the code of the skill ${step.skill} during the run, and the result is labelled untested`
    : "No tested tool does this step: the AI writes its code during the run, and the result is labelled untested";
  return `<span class="plan-step-flag untested" title="${escapeHtml(why)}">Untested code</span>`;
}

function planStepHtml(text, n, step, revised, showWhere, edit) {
  const parts = planStepParts(text);
  const settings = (step && step.settings) || {};
  const editable = !!(edit && step && step.id);
  const editAttrs = (what, key) => editable
    ? ` data-edit="${what}"${key ? ` data-key="${escapeHtml(key)}"` : ""} tabindex="0" role="button" `
      + `title="${what === "title" ? "Change the title" : `Change ${escapeHtml(key)}`}"`
    : "";
  const chips = Object.keys(settings).map((k) =>
    `<span class="plan-setting" title="Passed to the tool exactly as written">`
    + `<span class="plan-setting-k">${escapeHtml(k)}</span>`
    + `<span class="plan-setting-v${editable ? " editable" : ""}"${editAttrs("setting", k)}>`
    + `${escapeHtml(planSettingText(settings[k]))}</span></span>`).join("");
  const where = showWhere ? planRunsOnText(step && step.runs_on) : "";
  // The vetted skill the step uses: matched by AiScientist, handed to the agent with its guidance
  // and code when the step runs. The researcher can take it off.
  const skill = step && step.skill
    ? `<span class="plan-step-skill" title="This step uses the vetted skill ${escapeHtml(step.skill)}: its guidance and code go to the agent">`
      + `${planIcon("extension")}<span>${escapeHtml(step.skill)}</span>`
      + (editable && edit.remove ? `<button type="button" class="plan-skill-remove" data-skill-remove `
        + `aria-label="Run step ${n} without the skill" title="Run this step without the skill">${planIcon("close")}</button>` : "")
      + `</span>`
    : "";
  const meta = (chips || where || skill)
    ? `<div class="plan-step-meta">${skill}${chips}`
      + (where ? `<span class="plan-where">${planIcon(where.startsWith("Runs on") ? "dns" : "memory")}${escapeHtml(where)}</span>` : "")
      + `</div>`
    : "";
  // What the step should leave behind: declared by its skill, or by the active pipeline for its tool.
  // Checked after the step; a required one that is missing keeps the step from being accepted.
  const outs = step && Array.isArray(step.outputs) ? step.outputs : [];
  const outputs = outs.length
    ? `<div class="plan-step-outputs"><div class="plan-outputs-head">${planIcon("output")}<span>Expected outputs</span></div><ul>`
      + outs.map((o) => `<li><code title="Under the run's artifacts folder">${escapeHtml(o.path)}</code>`
        + `<span class="plan-output-kind">${escapeHtml(String(o.kind || "").replace("_", " "))}${o.required === false ? " · optional" : ""}</span>`
        + `<span class="plan-output-desc">${escapeHtml(o.description || "")}</span></li>`).join("")
      + `</ul></div>`
    : "";
  const open = !!parts.body && planStepOpen(text, revised);
  return `<li class="plan-step${revised ? " revised" : ""}${open ? " open" : ""}"`
    + (editable ? ` data-id="${escapeHtml(step.id)}"` : "") + `>`
    + `<span class="plan-step-num">${n}</span>`
    + `<div class="plan-step-main">`
    + `<div class="plan-step-head"><span class="plan-step-title${editable ? " editable" : ""}"${editAttrs("title")}>`
    + `${renderMarkdown(parts.title)}</span>`
    + (revised ? `<span class="plan-step-flag">Revised</span>` : "")
    + (step && step.tool ? `<code class="plan-step-tool">${escapeHtml(step.tool)}</code>` : "")
    + planUntestedFlag(step)
    + `<span class="plan-step-acts">`
    + (editable && edit.remove ? `<button type="button" class="plan-step-remove" data-step-remove `
      + `aria-label="Remove step ${n}" title="Remove this step">${planIcon("delete")}</button>` : "")
    + (parts.body ? `<button type="button" class="plan-step-more" data-step-more aria-expanded="${open}" `
      + `aria-label="Show the whole step" title="Show the whole step">${planIcon("expand_more")}</button>` : "")
    + `</span></div>`
    + (parts.body ? `<div class="plan-step-body">${planEmphasisHtml(renderMarkdown(parts.body), settings)}</div>` : "")
    + meta + outputs + `</div></li>`;
}

// One decimal from 1 up, two below it: an estimate is a range, and 2.667 hours claims more than it knows.
function estimateNumber(value) {
  return Number.isFinite(value)
    ? value.toLocaleString(undefined, { maximumFractionDigits: Math.abs(value) >= 1 ? 1 : 2 }) : "—";
}

function estimateRange(values, time = false) {
  if (!Array.isArray(values) || values.length !== 2 || !values.every(Number.isFinite)) return "—";
  const divisor = time ? (values[1] >= 3600 ? 3600 : 60) : 1;
  const [lo, hi] = values.map((v) => estimateNumber(v / divisor));
  return `${time ? "~" : ""}${lo === hi ? lo : `${lo}–${hi}`}` + (time ? (divisor === 3600 ? " hr" : " min") : "");
}

function estimateCell(value) {
  return String(value == null ? "" : value).replace(/[|\r\n]/g, " ");
}

function planEstimateMarkdown(estimate) {
  if (!estimate || !Array.isArray(estimate.steps)) return "";
  const currency = estimateCell(estimate.currency || "USD");
  const row = (entry, label, skill) => `| ${estimateCell(label)}${entry.rough ? " (rough)" : ""} | ${estimateCell(skill)} | `
    + `${estimateRange(entry.seconds, true)} | ${estimateRange(entry.cpu_hours)} | ${estimateRange(entry.gpu_hours)} | `
    + `${estimate.rates_configured ? estimateRange(entry.cost) + " " + currency : "—"} |`;
  return "\n\n**Estimated resources and cost**\n\n"
    + "| Step | Skill | Time | CPU hours | GPU hours | Est. cost |\n| --- | --- | --- | --- | --- | --- |\n"
    + estimate.steps.map((entry, i) => row(entry, entry.step || i + 1, entry.skill)).join("\n")
    + (estimate.total ? "\n" + row(estimate.total, "Total", "") : "")
    + "\n\nRough estimates have limited run history. "
    + (estimate.rates_configured ? "" : "Rates not configured: no cost is shown. ")
    + estimateCell(estimate.note || "HPC3 queue wait time is not included.") + "\n";
}

function runUsageMarkdown(msg) {
  const actual = msg.actual;
  const estimate = msg.estimate || {};
  const total = estimate.total || {};
  const cost = (value) => estimate.rates_configured
    ? `${value} ${estimateCell(estimate.currency || "USD")}` : "—";
  return "**Run resources and cost**\n\n| | Estimated | Actual |\n| --- | --- | --- |\n"
    + `| Time | ${estimateRange(total.seconds, true)} | ${estimateNumber(actual.seconds / 60)} min |\n`
    + `| CPU hours | ${estimateRange(total.cpu_hours)} | ${estimateNumber(actual.cpu_hours)} |\n`
    + `| GPU hours | ${estimateRange(total.gpu_hours)} | ${estimateNumber(actual.gpu_hours)} |\n`
    + `| Cost | ${cost(estimateRange(total.cost))} | ${cost(estimateNumber(actual.cost))} |\n\n`
    + (total.rough ? "Estimate: rough. " : "") + (actual.rough ? "Actual resource accounting: rough. " : "")
    + (estimate.rates_configured ? "" : "Rates not configured: no cost is shown. ")
    + "Time is summed tool compute time; model/report overhead and HPC3 queue wait time are excluded.";
}

// The estimate on the plan card: the same table the approved plan keeps in the chat, in a block of
// its own so a narrow side panel scrolls it sideways instead of widening.
function planEstimateHtml(estimate) {
  const md = planEstimateMarkdown(estimate);
  return md ? `<div class="plan-estimate">${renderMarkdown(md)}</div>` : "";
}

// The line under a plan's title: how many steps, how many settings the plan fixes, and where the
// steps run when that is the same for all of them (a step only carries its own line when the plan
// mixes places).
function planSub(agenda, docSteps) {
  const fixed = docSteps.reduce((n, st) => n + Object.keys((st && st.settings) || {}).length, 0);
  const places = [...new Set(docSteps.map((st) => planRunsOnText(st && st.runs_on)).filter(Boolean))];
  const onePlace = places.length === 1 ? places[0] : "";
  const sub = `${agenda.length} step${agenda.length === 1 ? "" : "s"}`
    + (fixed ? ` · ${fixed} setting${fixed === 1 ? "" : "s"} passed to the tools as written` : "")
    + (onePlace ? ` · ${onePlace.startsWith("Runs") ? onePlace.toLowerCase() : "runs as " + onePlace.replace(/ job /, " jobs ")}` : "");
  return { sub, onePlace };
}

// Which steps the last revision touched comes from the server (it has just applied the edit), so
// "what did my change actually do?" is answered on the plan instead of by diffing two plans by eye.
function planRevisedNote(revised) {
  if (!revised.size) return "";
  return `<div class="plan-note">${planIcon("edit")}<span>Step${revised.size > 1 ? "s" : ""} `
    + `${[...revised].sort((x, y) => x - y).join(", ")} ${revised.size > 1 ? "were" : "was"} revised — `
    + `every other step is unchanged.</span></div>`;
}

// The whole plan, as the side panel shows it. `opts.mode`:
//   "review" (default) — waiting for a decision: the document's editor and Run / Cancel;
//   "status"           — answered, the PI has it: `opts.status` instead of the buttons;
//   "saved"            — a plan this chat approved: read-only, with its document.
function planCardHtml(agenda, doc, revisedSteps, opts) {
  const o = opts || {};
  const mode = o.mode || "review";
  const docSteps = (doc && doc.steps) || [];
  const revised = new Set((revisedSteps || []).map(Number));
  const { sub, onePlace } = planSub(agenda, docSteps);
  const prose = agenda.map((s, i) => [s, i + 1]).filter(([s]) => planStepParts(s).body);
  const allOpen = prose.length > 0 && prose.every(([s, n]) => planStepOpen(s, revised.has(n)));
  const head = `<div class="plan-head"><span class="plan-head-icon">${planIcon("checklist")}</span>`
    + `<div><div class="plan-title">${mode === "saved" ? "Plan" : "Proposed plan"}</div><div class="plan-sub">${sub}</div></div>`
    + (prose.length ? `<button type="button" class="plan-expand" data-plan-expand>${allOpen ? "Collapse all" : "Expand all"}</button>` : "")
    + `</div>`;
  // A waiting plan with a document is changed on its cards; one step is never removed (to run
  // nothing, the plan is cancelled).
  const edit = mode === "review" && doc && doc.yaml ? { remove: agenda.length > 1 } : null;
  const steps = `<ol class="plan-steps">`
    + agenda.map((s, i) => planStepHtml(s, i + 1, docSteps[i], revised.has(i + 1), !onePlace, edit)).join("")
    + `</ol>`;
  if (mode !== "review") {
    return head + steps + planEstimateHtml(o.estimate) + planDocumentHtml(doc)
      + `<div class="plan-actions-row"><span class="plan-actions-status">${escapeHtml(o.status || "")}</span></div>`;
  }
  const tip = edit ? `<div class="plan-edit-tip">${planIcon("edit")}<span>Click a title or a value to change it, `
    + `or remove a step. Applied as written, without the model; the other steps stay as they are.</span></div>` : "";
  return head + planRevisedNote(revised) + tip + steps
    + planEstimateHtml(o.estimate)
    + planReviseHtml(o.words)
    + planEditorHtml(doc)
    + `<div class="plan-actions-row">`
    + `<button class="primary" data-plan="approve">${planIcon("play_arrow")}Run this plan</button>`
    + (doc && doc.yaml ? `<button class="secondary" data-plan="yaml">${planIcon("data_object")}<span>Edit as YAML</span></button>` : "")
    + `<button class="secondary quiet" data-plan="cancel">Cancel</button></div>`;
}

// The two ways to change a waiting plan sit together in the panel: say what to change, here or in
// the chat (the PI makes the change), or edit the document itself (Edit as YAML, applied by code).
// `words` is what was typed here and not sent, so a redraw of the panel keeps it.
function planReviseHtml(words) {
  return `<div class="plan-revise">`
    + `<div class="plan-revise-head">${planIcon("chat")}<span>Change it in words</span></div>`
    + `<div class="plan-revise-row"><textarea class="plan-revise-text" rows="2" `
    + `aria-label="Tell the PI what to change in the plan" `
    + `placeholder="e.g. drop the enrichment step, and cluster at resolution 0.8">${escapeHtml(words || "")}</textarea>`
    + `<button class="secondary small" data-plan="revise" disabled>${planIcon("send")}<span>Send</span></button></div>`
    + `<div class="plan-revise-draft" hidden>${planIcon("info")}<span>Your YAML edit is not applied yet: it is applied `
    + `first, and the PI changes the plan with it.</span></div>`
    + `<div class="plan-revise-tip">The PI changes the plan as it stands, with your own changes, and shows it to you `
    + `again; nothing runs until you run it. A question gets an answer and leaves the plan as it is. Same as replying `
    + `in the chat.</div></div>`;
}

// The document of a plan that is no longer waiting: there to read and copy, not to edit.
function planDocumentHtml(doc) {
  if (!doc || !doc.yaml) return "";
  return `<details class="plan-yaml plan-yaml-read"><summary class="plan-yaml-file">${planIcon("data_object")}plan.yaml</summary>`
    + `<div class="plan-yaml-code"><pre class="plan-yaml-hl">${planYamlHtml(doc.yaml)}</pre></div></details>`;
}

// The chat's datasets as the run will use them (main / sample / reference / extra input), and the
// ones it cannot use with the reason.
function planDatasetsHtml(doc) {
  const rows = ((doc && doc.datasets) || []).map((d) => {
    const use = d.use === "sample" ? "sample (QC'd together)" : (d.use || "extra input");
    return `<li><strong>${escapeHtml(d.name)}</strong> — ${escapeHtml(use)}</li>`;
  });
  for (const d of ((doc && doc.unusable_inputs) || [])) {
    rows.push(`<li><strong>${escapeHtml(d.name)}</strong> — cannot be used: ${escapeHtml(d.reason)}</li>`);
  }
  return rows.length ? `<section class="plan-datasets"><h4>Datasets for this run</h4><ul>${rows.join("")}</ul></section>` : "";
}

// The plan as the chat shows it: the step titles and the tool each one runs, with the decision
// beside them. The prose, the settings and the document are one click away in the side panel's Plan
// tab, so the chat stays a conversation and a long plan does not push it off the screen. `opts`:
// `pending` (waiting: Run / Open plan / Cancel), else `status` (what happened to it) with View plan;
// `revised`, `problems` (why the last edit of the document was refused), `dirty` (the document holds
// an edit that was not applied, so Run would run the plan without it).
function planEntryHtml(agenda, doc, opts) {
  const o = opts || {};
  const docSteps = (doc && doc.steps) || [];
  const revised = new Set((o.revised || []).map(Number));
  const { sub } = planSub(agenda, docSteps);
  const rows = agenda.map((s, i) => `<li class="plan-entry-step${revised.has(i + 1) ? " revised" : ""}">`
    + `<span class="plan-entry-num">${i + 1}</span>`
    + `<span class="plan-entry-title">${escapeHtml(planStepParts(s).title.replace(/[*`]/g, ""))}</span>`
    + (docSteps[i] && docSteps[i].tool ? `<code class="plan-step-tool">${escapeHtml(docSteps[i].tool)}</code>` : "")
    + (docSteps[i] && docSteps[i].skill
      ? `<span class="plan-step-skill" title="Uses the vetted skill ${escapeHtml(docSteps[i].skill)}">${planIcon("extension")}<span>${escapeHtml(docSteps[i].skill)}</span></span>` : "")
    + planUntestedFlag(docSteps[i])
    + `</li>`).join("");
  const refused = (o.problems || []).length
    ? `<div class="plan-note bad">${planIcon("error")}<span>Your edit was not applied — the plan is unchanged. Open the plan to see why.</span></div>`
    : "";
  const actions = o.pending
    ? `<button class="primary" data-plan="approve"${o.dirty ? ' disabled title="Apply or reset your edit first"' : ""}>${planIcon("play_arrow")}Run this plan</button>`
      + `<button class="secondary" data-plan="open">${planIcon("right_panel_open")}<span>Open plan</span></button>`
      + `<button class="secondary quiet" data-plan="cancel">Cancel</button>`
      + `<span class="plan-actions-hint">or reply below to change it</span>`
    : `<span class="plan-actions-status">${escapeHtml(o.status || "")}</span>`
      + `<button class="secondary small" data-plan="open">${planIcon("right_panel_open")}<span>View plan</span></button>`;
  return `<div class="plan-head"><span class="plan-head-icon">${planIcon("checklist")}</span>`
    + `<div><div class="plan-title">${o.pending ? "Proposed plan" : "Plan"}</div><div class="plan-sub">${sub}</div></div></div>`
    + (o.pending ? planRevisedNote(revised) + refused : "")
    + planDatasetsHtml(doc)
    + `<ol class="plan-entry-steps">${rows}</ol>`
    + `<div class="plan-actions-row">${actions}</div>`;
}

// The plan document's editor. Present only when the server sends the plan as a document (`plan`
// on the plan prompt); a server that does not gets the card without it. An edit is applied by the
// server's code, not by a model: a step that was not touched cannot change, and an edit that cannot
// be applied is refused with its reasons while the text that was typed stays in the box.
function planEditorHtml(doc) {
  if (!doc || !doc.yaml) return "";
  const problems = state.planProblems || [];
  // A refused edit reopens with what was typed, not with the plan it failed to change. So does a
  // card that is drawn again for the same document (keepPlanDraft): the text was never applied,
  // and the textarea is the only place it was.
  const mine = state.planDraftBase === doc.yaml;
  const draft = (state.planDraftYaml && (problems.length || mine)) ? state.planDraftYaml : "";
  const text = draft || doc.yaml;
  // Open where the person left it open, and whenever it holds text that is not applied: "Run this
  // plan" is disabled then, and the reason has to be on screen.
  const open = problems.length > 0 || !!draft || (mine && !!state.planEditorOpen);
  const rows = Math.max(8, Math.min(26, text.split("\n").length + 1));
  const banner = problems.length
    ? `<div class="plan-yaml-problems">${planIcon("error")}<div><strong>Your edit was not applied — the plan is unchanged.</strong>`
      + `<ul>${problems.map((x) => `<li>${escapeHtml(x)}</li>`).join("")}</ul></div></div>`
    : "";
  return `<div class="plan-yaml${open ? "" : " hidden"}">`
    + `<div class="plan-yaml-bar"><span class="plan-yaml-file">${planIcon("data_object")}plan.yaml</span>`
    + `<span class="plan-yaml-tip">Change a setting, reword, remove, reorder or add a step. `
    + `Your edit is applied exactly as written; nothing else in the plan changes.</span></div>`
    + banner
    // The text is typed into the textarea and read from the highlighted copy under it: the textarea's
    // own text is transparent, and the copy follows its scrolling (wirePlanEditor).
    + `<div class="plan-yaml-code plan-yaml-stack">`
    + `<pre class="plan-yaml-hl" aria-hidden="true">${planYamlHtml(text)}</pre>`
    + `<textarea class="plan-yaml-text" rows="${rows}" spellcheck="false" autocapitalize="off" `
    + `autocomplete="off" aria-label="The plan as YAML">${escapeHtml(text)}</textarea></div>`
    + `<div class="plan-yaml-actions"><button class="secondary small" data-plan="edit" disabled>Apply edits</button>`
    + `<button class="secondary quiet small" data-plan="reset">Reset</button></div></div>`;
}

// The plan document as text to read: keys quiet, and what a reviewer looks for — each step's id and
// title, its tool, the settings passed to it — in bold. Line by line, character for character: the
// editor draws this under its own (transparent) text, so it must never add or drop one.
function planYamlHtml(text) {
  let block = -1;      // column of the key whose block scalar (`does: >-`) the deeper lines belong to
  let settings = -1;   // column of `settings:`; the keys under it are the tool's arguments
  const VALUE = { id: "y-id", title: "y-title", tool: "y-tool", plan: "y-meta", request: "y-meta", runs_on: "y-meta" };
  return String(text == null ? "" : text).split("\n").map((line) => {
    const col = line.length - line.trimStart().length;
    if (block >= 0 && (!line.trim() || col > block)) return line.trim() ? `<span class="y-prose">${escapeHtml(line)}</span>` : escapeHtml(line);
    block = -1;
    if (line.trimStart().startsWith("#")) return `<span class="y-c">${escapeHtml(line)}</span>`;
    const m = /^(\s*)(-\s+)?([^\s:#"'][^:#]*?):(?=\s|$)(\s*)(.*)$/.exec(line);
    if (!m) return escapeHtml(line);
    const [, sp, dash = "", key, gap, rest] = m;
    const keyCol = sp.length + dash.length;
    if (settings >= 0 && keyCol <= settings) settings = -1;
    const inSettings = settings >= 0;
    if (key === "settings" && !rest) settings = keyCol;
    const c = /^([^"'#]*?)(\s+#.*)$/.exec(rest);
    const value = c ? c[1] : rest;
    const scalar = /^[>|][+-]?\d*$/.test(value);
    if (scalar) block = keyCol;
    const cls = inSettings ? "y-val" : scalar ? "y-ind" : (VALUE[key] || "y-v");
    return escapeHtml(sp)
      + (dash ? `<span class="y-d">-</span>${escapeHtml(dash.slice(1))}` : "")
      + `<span class="${inSettings ? "y-sk" : "y-k"}">${escapeHtml(key)}</span><span class="y-p">:</span>${escapeHtml(gap)}`
      + (value ? `<span class="${cls}">${escapeHtml(value)}</span>` : "")
      + (c ? `<span class="y-c">${escapeHtml(c[2])}</span>` : "");
  }).join("\n");
}

// ---- changing the plan on its cards ----------------------------------------------------------------
// A title, a setting's value or a removed step is written into the plan DOCUMENT, line by line, and
// sent as an edit: the server parses it, checks it against the tools and applies it by code, with no
// model, exactly like an edit made in the YAML editor. Every other line stays as the server wrote it,
// so the edit holds this one change and nothing else.

function planYamlUnquote(text) {
  const s = String(text == null ? "" : text).trim();
  if (s.startsWith('"')) { try { return String(JSON.parse(s)); } catch { return s; } }
  if (s.startsWith("'") && s.endsWith("'") && s.length > 1) return s.slice(1, -1).replace(/''/g, "'");
  return s;
}

const PLAN_YAML_NUMBER = /^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$/;

// A number as the server's YAML reader reads one: `1e-07` would be a STRING to it, `1.0e-07` a float.
function planYamlNumber(n, float) {
  if (!float && Number.isInteger(n)) return String(n);
  const s = String(n).toLowerCase();
  if (s.includes("e")) {
    let [m, e] = s.split("e");
    if (!m.includes(".")) m += ".0";
    if (!/^[-+]/.test(e)) e = "+" + e;
    return `${m}e${e}`;
  }
  return s.includes(".") ? s : s + ".0";
}

function planYamlItem(text) {
  if (PLAN_YAML_NUMBER.test(text)) return planYamlNumber(Number(text), /[.eE]/.test(text));
  if (/^(true|false)$/i.test(text)) return text.toLowerCase();
  return JSON.stringify(text);
}

// What was typed for a setting, written as the same kind of value the document had there: a number
// stays a number (and a float a float), a list a list, true/false a boolean; anything else is a
// quoted string. {text} or {error}.
function planYamlValue(literal, typed) {
  const raw = String(typed == null ? "" : typed).trim();
  const was = String(literal == null ? "" : literal).trim();
  if (/^\[.*\]$/.test(was)) {
    const items = raw.split(",").map((x) => x.trim()).filter(Boolean);
    return items.length ? { text: "[" + items.map(planYamlItem).join(", ") + "]" }
      : { error: "Give at least one value, separated by commas." };
  }
  if (/^(true|false)$/i.test(was)) {
    return /^(true|false)$/i.test(raw) ? { text: raw.toLowerCase() } : { error: "true or false is expected." };
  }
  if (PLAN_YAML_NUMBER.test(was)) {
    return raw && PLAN_YAML_NUMBER.test(raw) ? { text: planYamlNumber(Number(raw), /[.eE]/.test(was)) }
      : { error: "A number is expected." };
  }
  return raw ? { text: JSON.stringify(raw) } : { error: "A value is expected." };
}

// The plan document with one change made to it. `change`: {id, title} | {id, setting, value} (the
// value as typed) | {id, remove: true} | {id, skill: null} (the step goes without its skill). {yaml} or {error}: a value of the wrong kind, or a document
// edited by hand into a shape where the line is not where the server writes it.
function planYamlEdit(yaml, change) {
  const lines = String(yaml == null ? "" : yaml).split("\n");
  const indent = (l) => l.length - l.trimStart().length;
  const start = lines.findIndex((l) => {
    const m = /^\s*-\s+id:\s*(.*?)\s*$/.exec(l);
    return !!m && planYamlUnquote(m[1]) === String(change.id);
  });
  if (start < 0) return { error: "This step is no longer in the plan document." };
  const dash = indent(lines[start]);
  const keyCol = lines[start].indexOf("id:");
  let end = start + 1;
  while (end < lines.length && (!lines[end].trim() || indent(lines[end]) > dash)) end += 1;
  while (end > start + 1 && !lines[end - 1].trim()) end -= 1;
  if (change.remove) {
    lines.splice(start, end - start);
    return { yaml: lines.join("\n") };
  }
  if (change.skill === null) {
    for (let i = start + 1; i < end; i += 1) {
      if (indent(lines[i]) === keyCol && /^\s*skill:/.test(lines[i])) {
        lines.splice(i, 1);
        return { yaml: lines.join("\n") };
      }
    }
    return { error: "The skill is not where the plan document keeps it: delete it in the YAML editor." };
  }
  if (change.title !== undefined) {
    const title = String(change.title).replace(/\s+/g, " ").trim();
    if (!title) return { error: "A step needs a title." };
    for (let i = start + 1; i < end; i += 1) {
      const m = /^(\s*)title:\s*(.*)$/.exec(lines[i]);
      if (!m || indent(lines[i]) !== keyCol) continue;
      if (/^[>|]/.test(m[2])) break;
      lines[i] = `${m[1]}title: ${JSON.stringify(title)}`;
      return { yaml: lines.join("\n") };
    }
    return { error: "The title is not where the plan document keeps it: change it in the YAML editor." };
  }
  let settings = -1;
  for (let i = start + 1; i < end; i += 1) {
    if (settings < 0) {
      if (indent(lines[i]) === keyCol && /^\s*settings:\s*$/.test(lines[i])) settings = keyCol;
      continue;
    }
    if (lines[i].trim() && indent(lines[i]) <= settings) break;
    const m = /^(\s*)("[^"]*"|'[^']*'|[^\s:#"'][^:#]*?):(\s+)(.*)$/.exec(lines[i]);
    if (!m || planYamlUnquote(m[2]) !== String(change.setting)) continue;
    const c = /^["']/.test(m[4]) ? null : /^(.*?)(\s+#.*)$/.exec(m[4]);
    const value = planYamlValue(c ? c[1] : m[4], change.value);
    if (value.error) return value;
    lines[i] = `${m[1]}${m[2]}:${m[3]}${value.text}${c ? c[2] : ""}`;
    return { yaml: lines.join("\n") };
  }
  return { error: `${change.setting} is not where the plan document keeps it: change it in the YAML editor.` };
}

// Make a change on the cards of the plan waiting in chat `c`, and send it. An edit typed in the YAML
// editor and not applied yet is part of what the person has: the change is made on it, and both go
// together. Returns why it could not be made, or "".
function commitPlanChange(c, change) {
  const doc = c && c.plan && c.plan.kind === "plan" ? c.plan.doc : null;
  if (!doc || !doc.yaml) return "This plan has no document to change.";
  const out = planYamlEdit(c.planDraftYaml || doc.yaml, change);
  if (out.error) return out.error;
  submitPlan("edit", "", out.yaml, c);
  return "";
}

// A title or a setting's value turned into a text box. Enter (or leaving the box) applies a change,
// Escape puts the value back. A value that cannot be written stays open with the reason on Enter.
function planInlineEdit(li, el, initial, label, commit) {
  if (el.querySelector("input")) return;
  const before = el.innerHTML;
  const input = document.createElement("input");
  input.className = "plan-inline-input";
  input.value = initial;
  input.setAttribute("aria-label", label);
  input.spellcheck = false;
  if (el.classList.contains("plan-setting-v")) input.style.width = `${Math.max(6, String(initial).length + 2)}ch`;
  el.innerHTML = "";
  el.appendChild(input);
  el.classList.add("editing");
  input.focus();
  input.select();
  let done = false;
  const restore = () => { el.innerHTML = before; el.classList.remove("editing"); };
  const finish = (save, leaving) => {
    if (done) return;
    if (save && input.value.trim() !== String(initial).trim()) {
      // Sending draws the panel again at once, which takes the focus from this box: `done` first,
      // so that blur does not send the change a second time.
      done = true;
      const why = commit(input.value);
      if (!why) return;
      if (!leaving) { done = false; planStepError(li, why); input.focus(); return; }
      restore();
      planStepError(li, why);
      return;
    }
    done = true;
    restore();
    planStepError(li, "");
  };
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); finish(true, false); }
    else if (ev.key === "Escape") { ev.preventDefault(); finish(false, false); }
  });
  // Leaving the box applies the change, but a box that left because the panel was drawn again (a
  // status line) is not the person leaving it: nothing is sent for that.
  input.addEventListener("blur", () => setTimeout(() => { if (input.isConnected) finish(true, true); }, 0));
}

function planStepError(li, why) {
  const main = li.querySelector(".plan-step-main");
  let line = main.querySelector(".plan-step-error");
  if (!why) { if (line) line.remove(); return; }
  if (!line) {
    line = document.createElement("div");
    line.className = "plan-step-error";
    line.setAttribute("role", "alert");
    main.appendChild(line);
  }
  line.textContent = why;
}

function wirePlanCardEdits(host, c, doc) {
  const steps = (doc && doc.steps) || [];
  host.querySelectorAll(".plan-step[data-id]").forEach((li) => {
    const id = li.dataset.id;
    const step = steps.find((s) => s.id === id) || {};
    li.querySelectorAll("[data-edit]").forEach((el) => {
      const isTitle = el.dataset.edit === "title";
      const key = el.dataset.key || "";
      const start = () => planInlineEdit(li, el,
        isTitle ? (step.title || el.textContent) : planSettingText((step.settings || {})[key]),
        isTitle ? "The step's title" : `The value of ${key}`,
        (value) => commitPlanChange(c, isTitle ? { id, title: value } : { id, setting: key, value }));
      el.addEventListener("click", start);
      el.addEventListener("keydown", (ev) => {
        if (ev.target !== el || (ev.key !== "Enter" && ev.key !== " ")) return;
        ev.preventDefault();
        start();
      });
    });
    const skillOff = li.querySelector("[data-skill-remove]");
    if (skillOff) {
      skillOff.addEventListener("click", () => {
        const why = commitPlanChange(c, { id, skill: null });
        if (why) planStepError(li, why);
      });
    }
    const remove = li.querySelector("[data-step-remove]");
    if (!remove) return;
    remove.addEventListener("click", () => {
      const main = li.querySelector(".plan-step-main");
      if (main.querySelector(".plan-step-confirm")) return;
      const ask = document.createElement("div");
      ask.className = "plan-step-confirm";
      ask.innerHTML = `<span>Remove this step from the plan?</span>`
        + `<button type="button" class="secondary small" data-yes>Remove</button>`
        + `<button type="button" class="secondary quiet small" data-no>Keep</button>`;
      main.appendChild(ask);
      ask.querySelector("[data-no]").addEventListener("click", () => ask.remove());
      ask.querySelector("[data-yes]").addEventListener("click", () => {
        const why = commitPlanChange(c, { id, remove: true });
        if (why) { ask.remove(); planStepError(li, why); }
      });
      ask.querySelector("[data-yes]").focus();
    });
  });
}

// The waiting plan: its entry in the chat (titles, Run / Open plan / Cancel) and the whole plan, with
// its document's editor, in the side panel.
function showPlanPanel(agenda) {
  const el = planCardShell("plan");
  el.classList.add("plan-sheet", "plan-entry");
  state.planAgenda = agenda || [];
  const c = activeChat();     // the buttons answer THIS chat's run, whatever is on screen later
  el.innerHTML = planEntryHtml(state.planAgenda, state.planDoc, { pending: true, revised: state.planRevisedSteps,
    problems: state.planProblems, dirty: !!state.planDraftYaml });
  wirePlanEntry(el, c);
  // The whole plan is in the side panel: it opens the first time each version of the plan is shown
  // in its chat (also when it arrived while another chat was on screen), not on every redraw.
  const seen = JSON.stringify([state.planAgenda, (state.planDoc && state.planDoc.yaml) || ""]);
  if (c && c.planShown !== seen) { c.planShown = seen; openPlanPanel(); }
  renderPlanPanel();
  $("chatInput").placeholder = "Tell the PI what to change (e.g. 'drop the enrichment step')…";
  $("chatStream").scrollTop = $("chatStream").scrollHeight;
  updateComposerButton();
}

function wirePlanEditor(el, c) {
  const panel = el.querySelector(".plan-yaml");
  if (!panel) return;
  const box = panel.querySelector(".plan-yaml-text");
  const shown = panel.querySelector(".plan-yaml-hl");
  const original = state.planDoc.yaml;
  const apply = panel.querySelector('[data-plan="edit"]');
  const runButtons = () => [el.querySelector('[data-plan="approve"]'),
    chatVisible(c) && state.planCardEl ? state.planCardEl.querySelector('[data-plan="approve"]') : null].filter(Boolean);
  const toggle = el.querySelector('[data-plan="yaml"]');
  const label = () => { toggle.querySelector("span:last-child").textContent = panel.classList.contains("hidden") ? "Edit as YAML" : "Hide YAML"; };
  // While the box holds an edit that has not been applied, "Run this plan" would run the plan
  // WITHOUT it. So the edit becomes the primary action and Run waits for Apply or Reset.
  const follow = () => { if (shown) { shown.scrollTop = box.scrollTop; shown.scrollLeft = box.scrollLeft; } };
  const sync = () => {
    if (shown) { shown.innerHTML = planYamlHtml(box.value); follow(); }
    const dirty = notePlanDraft(box.value, c);
    apply.disabled = !dirty;
    apply.classList.toggle("primary", dirty);
    apply.classList.toggle("secondary", !dirty);
    for (const run of runButtons()) {
      run.disabled = dirty;
      run.title = dirty ? "Apply or reset your edit first" : "";
    }
    refreshPlanRevise(el, c);
  };
  toggle.addEventListener("click", () => {
    panel.classList.toggle("hidden");
    notePlanEditorOpen(!panel.classList.contains("hidden"), c);
    label();
    if (!panel.classList.contains("hidden")) box.focus();
  });
  box.addEventListener("input", sync);
  box.addEventListener("scroll", follow);
  // Tab indents (YAML is indentation) instead of leaving the box.
  box.addEventListener("keydown", (ev) => {
    if (ev.key !== "Tab" || ev.shiftKey || ev.altKey || ev.ctrlKey || ev.metaKey) return;
    ev.preventDefault();
    box.setRangeText("  ", box.selectionStart, box.selectionEnd, "end");
    sync();
  });
  apply.addEventListener("click", () => submitPlan("edit", "", box.value, c));
  panel.querySelector('[data-plan="reset"]').addEventListener("click", () => {
    box.value = original;
    sync();
  });
  label();
  sync();
}

// The chat entry's own buttons. They answer THIS chat's run, whatever is on screen when clicked.
function wirePlanEntry(el, c) {
  const on = (name, fn) => { const b = el.querySelector(`[data-plan="${name}"]`); if (b) b.addEventListener("click", fn); };
  on("approve", () => submitPlan("approve", undefined, undefined, c));
  on("cancel", () => submitPlan("cancel", undefined, undefined, c));
  on("open", () => { state.planView = null; openPlanPanel(); renderPlanPanel(); });
}

// A plan this chat answered (a reply in words, an edit, a cancel, or a review the server closed):
// its entry, with what happened to it and "View plan", drawn again from the chat's record.
// It is not a card waiting for anything (`state.planCardEl` stays empty); the next prompt's card
// takes its place (planCardShell), so a review is one entry however many rounds it takes.
function drawAnsweredPlan(c = activeChat()) {
  const a = c && c.answered;
  if (!a || !chatVisible(c)) return;
  if (state.planAnsweredEl) state.planAnsweredEl.remove();
  const stream = $("chatStream");
  const el = document.createElement("div");
  el.className = "msg assistant plan-card plan-sheet plan-entry";
  el.dataset.prompt = "plan";
  el.innerHTML = planEntryHtml(a.agenda, a.doc, { status: a.status });
  stream.appendChild(el);
  wirePlanEntry(el, c);
  state.planAnsweredEl = el;
  state.planStatusEl = el.querySelector(".plan-actions-status");
}

// ---- the side panel's "Plan" tab ------------------------------------------------------------------
// What the panel shows for the chat on screen: a saved plan the person opened from the chat
// (`state.planView`), else the plan waiting in this chat, else the one it answered and the PI has,
// else the last plan this chat approved.
function chatPlanView(c = activeChat()) {
  if (state.planView) return { mode: "saved", ...state.planView };
  if (c && c.plan && c.plan.kind === "plan") {
    return { mode: "review", agenda: c.plan.agenda || [], doc: c.plan.doc || null, revised: c.plan.revised || [],
             estimate: c.plan.estimate || null };
  }
  if (c && c.answered) return { mode: "status", ...c.answered };
  const s = activeSession();
  const saved = s && [...s.messages].reverse().find((m) => m.kind === "plan" && m.plan);
  return saved ? { mode: "saved", ...saved.plan } : null;
}

function renderPlanPanel() {
  const host = $("planPanel");
  if (!host) return;
  const c = activeChat();
  const tab = $("sideTabPlan");
  if (tab) tab.classList.toggle("waiting", !!(c && c.plan && c.plan.kind === "plan"));
  const view = chatPlanView(c);
  if (!view) {
    host.innerHTML = '<div class="downloads-empty">No plan in this chat yet. When the PI proposes one it opens here: every step and the settings it fixes. You can change it in words, or edit it as a YAML document.</div>';
    return;
  }
  host.innerHTML = `<div class="plan-card plan-sheet plan-panel-card" data-prompt="plan">`
    + planCardHtml(view.agenda || [], view.doc || null, view.revised || [],
                   { mode: view.mode, status: view.status || "", estimate: view.estimate || null,
                     words: (c && c.planWords) || "" })
    + `</div>`;
  wirePlanSteps(host, view.agenda || []);
  if (view.mode !== "review") return;
  host.querySelector('[data-plan="approve"]').addEventListener("click", () => submitPlan("approve", undefined, undefined, c));
  host.querySelector('[data-plan="cancel"]').addEventListener("click", () => submitPlan("cancel", undefined, undefined, c));
  wirePlanRevise(host, c);
  wirePlanCardEdits(host, c, view.doc);
  wirePlanEditor(host, c);
}

// The panel's "Change it in words" box. Enter sends (Shift+Enter is a new line). An edit in the YAML
// editor that is not applied goes with the words (revisePlanInWords), and the box says so.
function wirePlanRevise(host, c) {
  const box = host.querySelector(".plan-revise-text");
  const send = host.querySelector('[data-plan="revise"]');
  if (!box || !send) return;
  const go = () => {
    const text = box.value.trim();
    if (!text || send.disabled) return;
    box.value = "";
    revisePlanInWords(c, text);
  };
  box.addEventListener("input", () => { c.planWords = box.value; refreshPlanRevise(host, c); });
  box.addEventListener("keydown", (ev) => {
    if (ev.key !== "Enter" || ev.shiftKey || ev.isComposing) return;
    ev.preventDefault();
    go();
  });
  send.addEventListener("click", go);
  refreshPlanRevise(host, c);
}

function refreshPlanRevise(host, c) {
  const box = host.querySelector(".plan-revise-text");
  const send = host.querySelector('[data-plan="revise"]');
  if (!box || !send) return;
  send.disabled = !box.value.trim();
  const note = host.querySelector(".plan-revise-draft");
  if (note) note.hidden = !(c && c.planDraftYaml);
}

// A step opens and closes by its chevron, or opens by a click on its two lines; "Expand all" does
// every step. What the person chose is kept per step text, so a redraw (a status line, a revision
// that left the step alone) does not fold it again.
function wirePlanSteps(host, agenda) {
  const items = [...host.querySelectorAll(".plan-step")];
  const setOpen = (li, i, open) => {
    li.classList.toggle("open", open);
    const more = li.querySelector("[data-step-more]");
    if (more) more.setAttribute("aria-expanded", String(open));
    state.planStepOpen.set(agenda[i], open);
  };
  items.forEach((li, i) => {
    const body = li.querySelector(".plan-step-body");
    const more = li.querySelector("[data-step-more]");
    if (!body || !more) return;
    more.addEventListener("click", () => { setOpen(li, i, !li.classList.contains("open")); fitPlanSteps(host); });
    body.addEventListener("click", () => {
      if (li.classList.contains("open") || !planStepFoldable(li) || String(window.getSelection() || "")) return;
      setOpen(li, i, true);
      fitPlanSteps(host);
    });
  });
  const all = host.querySelector("[data-plan-expand]");
  if (all) {
    all.addEventListener("click", () => {
      const open = items.filter(planStepFoldable).some((li) => !li.classList.contains("open"));
      items.forEach((li, i) => { if (planStepFoldable(li)) setOpen(li, i, open); });
      fitPlanSteps(host);
    });
  }
  // The panel slides open after it is drawn, and its width changes with the window: whether a step's
  // prose fits in two lines is measured again whenever its size does.
  if (!host.planFitObserver && typeof ResizeObserver === "function") {
    host.planFitObserver = new ResizeObserver(() => fitPlanSteps(host));
    host.planFitObserver.observe(host);
  }
  fitPlanSteps(host);
}

function planStepFoldable(li) {
  return !!li.querySelector(".plan-step-body") && !li.classList.contains("fits");
}

// Prose that fits in two lines has nothing to open, so its step has no chevron. Measured only while
// the panel is laid out: hidden, every step keeps its chevron.
function fitPlanSteps(host) {
  const items = [...host.querySelectorAll(".plan-step")];
  for (const li of items) {
    const body = li.querySelector(".plan-step-body");
    if (!body) continue;
    const lh = parseFloat(getComputedStyle(body).lineHeight);
    li.classList.toggle("fits", body.clientWidth > 0 && lh > 0 && body.scrollHeight <= lh * 2 + 2);
  }
  const all = host.querySelector("[data-plan-expand]");
  if (!all) return;
  const foldable = items.filter(planStepFoldable);
  all.hidden = !foldable.length;
  all.textContent = foldable.every((li) => li.classList.contains("open")) ? "Collapse all" : "Expand all";
}

// The right-hand panel holds two things, one at a time: the plan and the results.
function selectSideTab(name) {
  state.sideTab = name === "plan" ? "plan" : "results";
  const plan = state.sideTab === "plan";
  $("planPanel").classList.toggle("hidden", !plan);
  $("resultsPane").classList.toggle("hidden", plan);
  $("sideTabPlan").classList.toggle("active", plan);
  $("sideTabResults").classList.toggle("active", !plan);
  $("layout").classList.toggle("side-plan", plan);   // a plan is prose: the panel gets more room for it
}

// Open the side panel on the plan. Not remembered as the panel's own setting: a plan arriving must
// not turn "results panel collapsed" into "always open" for the next visit.
function openPlanPanel() {
  selectSideTab("plan");
  setPanel("right", true, false);
}

// The approved plan as the chat keeps it. `content` is what a model reading this conversation later
// is given (the steps, and the document with the settings that ran); `plan` is what the entry and
// the side panel are drawn from.
function planSavedMessage(p) {
  const docMd = planEstimateMarkdown(p.estimate) + ((p.doc && p.doc.yaml)
    ? "\n\n**Approved plan document**\n\n```yaml\n" + p.doc.yaml.trim() + "\n```"
    : "");
  return { role: "assistant", kind: "plan",
           content: "## 📋 Research plan\n\n" + p.agenda.map((s, i) => `${i + 1}. ${s}`).join("\n") + docMd,
           plan: { agenda: p.agenda, doc: p.doc || null, estimate: p.estimate || null, status: "Approved" } };
}

function showClarify(questions) {
  const c = activeChat();
  const el = planCardShell("clarify");
  const head = document.createElement("div");
  head.innerHTML = renderMarkdown("## ❓ The PI needs a quick decision");
  el.appendChild(head);
  for (const q of (questions || [])) {
    const wrap = document.createElement("div");
    wrap.className = "clarify-q";
    const qt = document.createElement("div");
    qt.className = "clarify-question";
    qt.textContent = q.question;
    wrap.appendChild(qt);
    const opts = document.createElement("div");
    opts.className = "clarify-options";
    for (const opt of (q.options || [])) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "clarify-chip";
      chip.textContent = opt;
      // Answer = "<question>: <option>" so the PI knows which question was answered.
      chip.addEventListener("click", () => submitPlan("revise", q.question + ": " + opt, undefined, c));
      opts.appendChild(chip);
    }
    wrap.appendChild(opts);
    el.appendChild(wrap);
  }
  $("chatInput").placeholder = "Answer the PI, or type your own…";
  $("chatStream").scrollTop = $("chatStream").scrollHeight;
  updateComposerButton();
}

// HITL decision point (DAG planner): a mid-run methodological fork where the user picks the
// approach with real context. Reuses the SAME plan_event round-trip as plan review (answered via
// /api/lab/plan), so there is no new backend path — only the card wording differs.
function showDecision(goal, options) {
  const c = activeChat();
  const el = planCardShell("decision");
  const head = document.createElement("div");
  head.innerHTML = renderMarkdown("## 🔀 Decision point — how should we proceed?");
  el.appendChild(head);
  const q = document.createElement("div");
  q.className = "clarify-question";
  q.textContent = goal || "";
  el.appendChild(q);
  const opts = document.createElement("div");
  opts.className = "clarify-options";
  for (const opt of (options || [])) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "clarify-chip";
    chip.textContent = opt;
    chip.addEventListener("click", () => submitDecision(opt, c));
    opts.appendChild(chip);
  }
  // Always offer to defer to the agent, even when the plan gave no explicit options.
  const defer = document.createElement("button");
  defer.type = "button";
  defer.className = "clarify-chip ghost";
  defer.textContent = "Let the agent decide";
  defer.addEventListener("click", () => submitDecision("", c));
  opts.appendChild(defer);
  el.appendChild(opts);
  $("chatInput").placeholder = "Pick an option — or leave it to the agent…";
  $("chatStream").scrollTop = $("chatStream").scrollHeight;
  updateComposerButton();
}

// choice "" = proceed with the agent's judgment; a non-empty string = the chosen approach. Sent as a
// "revise" (the backend's decision_review treats any non-cancel action as proceed, choice=feedback).
async function submitDecision(choice, c = activeChat()) {
  if (!state.connectionId || !c) return;
  finalizePlanCard(choice ? `✓ Chose: ${choice}` : "✓ Proceeding — agent decides", null, c);
  releasePlanPrompt(c);
  if (c.run) { c.run.startedAt = Date.now(); c.run.statusLabel = ""; }
  if (chatVisible(c)) {
    $("chatInput").placeholder = DEFAULT_PLACEHOLDER;
    mountStream();
    repaintWorking();
  }
  updateComposerButton();
  try {
    const res = await fetch("/api/lab/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, conversation_id: c.sid, action: "revise", feedback: choice || "" }),
    });
    if (await planAnswerStale(res)) { planNotDelivered(c); return; }
    toast(choice ? "Decision recorded — continuing…" : "Continuing…");
  } catch (err) { toast("Failed to send decision: " + err.message); }
}

// /api/lab/plan answers {status: "not_delivered"} (older gateways: "stale") when neither a run of
// this chat nor a saved plan is waiting for the answer any more: the answer went nowhere.
async function planAnswerStale(res) {
  if (!res || !res.ok) return false;
  const body = await res.json().catch(() => null);
  return !!body && (body.status === "not_delivered" || body.status === "stale");
}

// Said on the card and in the chat, and the chat stops showing a run: without this the card kept
// "Sent to the PI — reading your reply…" for good, waiting for a run that was gone. `message`: the
// server's own wording, when it gave one.
function planNotDelivered(c, message) {
  const text = !message ? "Not delivered — this chat's run is no longer waiting for an answer. Send your request again."
    : /^Not delivered\b/i.test(message) ? message : "Not delivered: " + message;
  if (c.answered) c.answered.status = text;
  if (chatVisible(c) && state.planStatusEl && state.planStatusEl.isConnected) state.planStatusEl.textContent = text;
  if (c.running) { finishAssistantStream(c); setChatRunning(c, false); }
  if (chatVisible(c)) renderPlanPanel();
  toast(text);
}

// Resolve the live plan/clarify card with a short status line (drop the buttons), and on
// approval persist the plan to history so it survives a reload.
function finalizePlanCard(note, persist, c = activeChat()) {
  const p = c && c.plan;
  const saved = !persist ? null : typeof persist === "string" ? { role: "assistant", content: persist } : persist;
  // A plan answered in words, by an edit or by a cancel stays readable — in its entry and in the side
  // panel — while the PI works on the answer. An approved plan is saved instead (below).
  if (c && p && p.kind === "plan") c.answered = saved ? null : { agenda: p.agenda || [], doc: p.doc || null, revised: p.revised || [], status: note };
  if (state.planCardEl && chatVisible(c) && p && p.kind === "plan") {
    const entry = state.planCardEl;
    if (saved && saved.kind === "plan") {
      const fresh = messageEl(saved);
      if (typeof entry.replaceWith === "function") entry.replaceWith(fresh); else entry.remove();
    } else {
      entry.innerHTML = planEntryHtml(p.agenda || [], p.doc || null, { status: note });
      wirePlanEntry(entry, c);
      state.planStatusEl = entry.querySelector(".plan-actions-status");
      state.planAnsweredEl = entry;
    }
    state.planCardEl = null;
  } else if (state.planCardEl && chatVisible(c)) {
    // A resolved card is a record: its editor goes with its buttons, or "Apply edits" would stay
    // clickable on a plan that is no longer waiting for anything.
    const editor = state.planCardEl.querySelector(".plan-yaml");
    if (editor) editor.remove();
    const row = state.planCardEl.querySelector(".plan-actions-row");
    if (row) row.innerHTML = `<span class="plan-actions-status">${escapeHtml(note)}</span>`;
    // Keep a handle to the STATUS line after the card is resolved. The card is the only thing on
    // screen while the PI works, and what it says is the user's only signal about which of the two
    // things is happening (answering vs re-planning) — see setPlanStatus.
    state.planStatusEl = row ? row.querySelector(".plan-actions-status") : null;
    state.planCardEl = null;
  }
  if (saved && c) pushToSession(c.sid, saved);
  if (chatVisible(c)) renderPlanPanel();
}

// Correct the resolved card's status line as the server learns what the reply actually was.
// The client cannot classify question-vs-change itself (the rule lives in the lab, and a second
// copy here would drift from it), so it opens with a NEUTRAL "sent to the PI" and the server
// upgrades it. The old unconditional "Sent changes to the PI — re-planning…" asserted the wrong
// one of the two for every question asked, for the 10-20 minutes an answer takes.
function setPlanStatus(text) {
  if (state.planStatusEl && text) state.planStatusEl.textContent = text;
  const c = activeChat();
  if (c && c.answered && text) { c.answered.status = text; renderPlanPanel(); }
}

function hidePlanPanel(c = activeChat()) {
  // plan_done (any resolution). Card finalization happens in submitPlan; here we only
  // clear the pending state + restore the composer (idempotent).
  if (!c) return;
  // A plan still held here was not answered on this page: the run stopped or ended.
  const closed = c.plan && c.plan.kind === "plan" ? c.plan : null;
  if (closed) c.answered = { agenda: closed.agenda || [], doc: closed.doc || null, revised: closed.revised || [],
                             status: "This plan is no longer waiting for a decision." };
  releasePlanPrompt(c);
  if (closed && chatVisible(c)) { removePlanCard(); drawAnsweredPlan(c); renderPlanPanel(); }
  if (chatVisible(c)) $("chatInput").placeholder = DEFAULT_PLACEHOLDER;
  updateComposerButton();
}

// A change to the waiting plan asked for in words, typed in the composer or in the side panel's box:
// it shows in the chat as the person's message and goes to the PI as feedback. The PI answers a
// question and sends the plan back as it was, or makes the change on the plan document and sends
// it back for review; nothing runs until the person runs it. Words are always about the plan as the
// person has it: an edit of the document they have not applied yet goes with them, and the server
// applies it first (by code) and makes the change on the edited plan.
function revisePlanInWords(c, text) {
  c.planWords = "";
  if (chatVisible(c)) appendUserMessage(text);
  submitPlan("revise", text, c.planDraftYaml || "", c);
}

// action: "approve" | "revise" | "edit" | "cancel"; feedback only for "revise", planYaml only
// for "edit" (the plan document as the user edited it).
async function submitPlan(action, feedback, planYaml, c = activeChat()) {
  if (!state.connectionId || !c) return;
  // The approved document is saved with the plan: it is what the run was held to.
  const plan = c.plan || {};
  const draft = { planDraftYaml: action === "edit" ? (planYaml || "") : c.planDraftYaml,
                  planDraftBase: c.planDraftBase, planEditorOpen: c.planEditorOpen };
  const restore = () => {
    if (!plan.saved || c.plan) return;
    Object.assign(c, draft);
    holdPlanPrompt(c, plan.kind, plan, { run_id: plan.run, saved: true, revised: plan.revised });
  };
  const savedPlan = (plan.agenda && plan.agenda.length) ? planSavedMessage(plan) : null;
  // An edit that is sent stays as the draft until the server answers: refused, it comes back in the
  // editor. A reply in words leaves the draft alone too — the PI may answer a question and send the
  // same plan again. Only a plan that is run or cancelled has no use for it (keepPlanDraft drops it
  // when the plan changes).
  if (action === "edit") c.planDraftYaml = planYaml || "";
  else if (action !== "revise") dropPlanDraft(c);
  if (action === "approve") {
    if (c.run) { c.run.startedAt = Date.now(); c.run.statusLabel = ""; }
    finalizePlanCard(plan.saved ? "Submitting approval…" : "▶ Approved — running…", plan.saved ? null : savedPlan, c);
    // Re-anchor the live bubble below the just-approved plan so the run's activity
    // (reasoning / running code / report) streams in AFTER it, where the user is looking.
    if (chatVisible(c)) { mountStream(); repaintWorking(); }
  }
  else if (action === "cancel") finalizePlanCard("✕ Cancelled", null, c);
  else if (action === "edit") finalizePlanCard("✎ Applying your edit…", null, c);
  // Neutral until the server says which it was: a reply may be a QUESTION (answered, plan
  // untouched) or a CHANGE (re-planned). Claiming "re-planning" up front was wrong for every
  // question, and stayed wrong on screen for the whole 10-20 min the answer took.
  else finalizePlanCard("✎ Sent to the PI — reading your reply…", null, c);
  releasePlanPrompt(c);
  if (chatVisible(c)) renderPlanPanel();   // now without the prompt: the answered plan, read-only
  if (chatVisible(c)) $("chatInput").placeholder = DEFAULT_PLACEHOLDER;
  if (chatVisible(c)) repaintWorking();
  updateComposerButton();
  try {
    const body = { connection_id: state.connectionId, conversation_id: c.sid, run_id: plan.run || "", action, feedback: feedback || "",
                   plan_yaml: action === "edit" || action === "revise" ? (planYaml || "") : "",
                   tz: typeof userTz === "function" ? userTz() : "" };
    const postPlan = () => fetch("/api/lab/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    let res = await postPlan();
    let reply = await res.json().catch(() => ({}));
    if (!res.ok && reply.needs_budget_confirm
        && confirm(`${reply.error}\n\nThe figure is an estimate; the Claude Console has the actual spend.`)) {
      body.budget_ack = true;
      res = await postPlan();
      reply = await res.json().catch(() => ({}));
    }
    // No plan of this chat is waiting any more (it ended, or was answered elsewhere): the answer
    // went nowhere. A Cancel of such a card has nothing left to cancel.
    if (res.ok && (reply.status === "not_delivered" || reply.status === "stale")) {
      if (action !== "cancel") planNotDelivered(c, reply.message);
      return;
    }
    // Refused (the run limit, the budget, a plan already being handled): the card and the
    // editor's draft come back, so the answer can be sent again.
    if (!res.ok) {
      const message = reply.message || reply.error || `The server refused the answer (${res.status}).`;
      const note = /^Not delivered\b/i.test(message) ? message : "Not delivered: " + message;
      restore();
      if (chatVisible(c)) setPlanStatus(note);
      else if (c.answered) c.answered.status = note;
      toast(note);
      return;
    }
    if (action === "approve" && plan.saved && savedPlan) pushToSession(c.sid, savedPlan);
    if (action === "approve") toast("Plan approved — running…");
    else if (action === "cancel") toast("Plan cancelled.");
    else if (action === "edit") toast("Edit sent — checking it…");
    else toast("Sent to the PI — re-planning…");
  } catch (err) {
    restore();
    toast(isUnreachable(err) ? "Can't reach the local server." : "Plan submit failed: " + err.message);
  }
}

// ---- uploads ------------------------------------------------------------------------------------
// A file reaches the gateway over tus (resumable: 8 MiB chunks, each with its SHA-256, and the
// whole-file SHA-256 with the last one); the gateway then copies it to HPC3 in the background
// (gateway/staging.py). One card in the Datasets tab follows an upload through both halves, and a
// folder is one card. Cards come back after a page reload: copies still running or failed are read
// from the server, uploads the browser had not finished from its own storage.
const UPLOAD_CHUNK = 8 * 1024 * 1024;
const TUS_CONCURRENCY = 3;
// A broken connection is tried again by itself for about five minutes. The count starts over
// whenever a chunk got through, so this is per outage, not per upload. With seven steps (69 s) a
// 20 s outage used five of them and one more fault ended the upload (measured 2026-10-12).
const TUS_RETRY_DELAYS = [0, 1000, 3000, 5000, 10000, 20000, 30000, 30000, 60000, 60000, 60000];
// Uploads that have a retry pending. When the link is back (the browser says so, or the page's
// WebSocket reconnected) they try at once instead of sitting out the rest of a long delay, and the
// ones that had given up continue.
const tusKicks = new Set();
function uploadsLinkBack() {
  for (const kick of [...tusKicks]) kick();
  for (const u of [...state.uploads]) {
    if (u.status !== "error" || !u.lost || u.jobId || u.serverFolder) continue;   // only what the browser still owes
    if (u.kind === "folder" && u.files) uploadFolder(u.files);
    else if (u.kind !== "folder" && u.file) uploadDataset(u.file);
  }
}
window.addEventListener("online", uploadsLinkBack);
// Say what stopped an upload in words the user can act on. `where` is " at 31%" or "", `next` is
// what continuing takes. The library's own message stays available as the card's tooltip.
function uploadFailure(err, where, next) {
  const res = tusResponseOf(err);
  const status = res ? res.getStatus() : 0;
  let said = "";
  if (res) { try { said = JSON.parse(res.getBody() || "{}").error || ""; } catch { /* not JSON */ } }
  const detail = (err && err.message) || "";
  const noAnswer = !!err && (err instanceof TypeError || (!!err.originalRequest && !status));
  if (noAnswer) return { lost: true, detail, text: `The connection was lost${where}. ${next}` };
  if (status === 507) return { lost: false, detail, text: "The server's disk is too full for this file." };
  if (status >= 500) return { lost: true, detail, text: `The server did not answer (HTTP ${status})${where}. ${next}` };
  if (status === 401) return { lost: false, detail, text: "You are signed out. Sign in again, then continue the upload." };
  if (status === 408 || status === 423) {   // the server keeps the upload: no bytes came, or no session holds it
    return { lost: true, detail, text: /connect/i.test(said)
      ? `Upload paused${where}: this session is not connected (the server restarted, or you disconnected). Connect to HPC3 again. ${next}`
      : `The connection was lost${where}. ${next}` };
  }
  if (status === 413) return { lost: false, detail, text: "The file is larger than the server accepts." };
  if (status === 460) return { lost: false, detail, text: `A piece of the file kept arriving damaged${where}. ${next}` };
  if (status === 404 && /connection/i.test(said)) {
    return { lost: false, detail, text: "The server no longer knows this session (it was restarted, or you were disconnected). Connect to HPC3 again, then continue the upload." };
  }
  if (status) return { lost: false, detail, text: said ? `The server refused the upload: ${said}` : `The server refused the upload (HTTP ${status}).` };
  return { lost: false, detail: "", text: detail.replace(/^tus: /, "") || "upload failed" };
}
const STAGING_POLL_MS = 15000;      // how long to wait for a WebSocket event before asking the server
const UPLOAD_ACTIVE = ["queued", "uploading", "staging", "verifying", "unpacking"];
const ARCHIVE_RE = /\.(zip|tar|tar\.gz|tgz)$/i;
let tusRunning = 0;
const tusWaiting = [];
function uploadSlot() {
  if (tusRunning < TUS_CONCURRENCY) { tusRunning++; return Promise.resolve(); }
  return new Promise((resolve) => tusWaiting.push(resolve));
}
function releaseUploadSlot() {
  tusRunning--;
  if (tusWaiting.length) { tusRunning++; tusWaiting.shift()(); }
}
function tusId(url) { return (url || "").split("/").pop(); }
function tusHeaders() { return { "Tus-Resumable": "1.0.0", "X-Connection-ID": state.connectionId || "" }; }
function postJson(url, body) {
  return fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connection_id: state.connectionId, ...body }) });
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// 20 GB reads badly as "21474.8 MB"
const fmtUploadSize = (n) => ((n || 0) >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : (n || 0) >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : (n || 0) >= 1e3 ? `${(n / 1e3).toFixed(1)} kB` : `${n || 0} B`);

// -- speed and time left ----------------------------------------------------------------------------
// Each card measures its own bytes over the last seconds, in both halves: the browser sending to the
// server ("uploading") and the server copying to HPC3 ("staging"). An average since the start would
// go on reporting the speed of a link that has since slowed down or stopped.
const UPLOAD_PACE_WINDOW_MS = 15000;    // the stretch the speed is averaged over
const UPLOAD_PACE_STALE_MS = 20000;     // no new byte for this long: there is no speed to report
const UPLOAD_PACE_PHASES = ["uploading", "staging"];
const uploadPaces = new Map();          // card id -> { phase, samples: [[ms, bytes], …] }
let uploadPaceTimer = null;
const fmtUploadSpeed = (bps) => (bps >= 1e9 ? `${(bps / 1e9).toFixed(2)} GB/s`
  : bps >= 1e6 ? `${(bps / 1e6).toFixed(1)} MB/s` : `${Math.max(1, Math.round(bps / 1e3))} kB/s`);
function fmtUploadEta(seconds) {
  const s = Math.max(1, Math.round(seconds));
  if (s < 60) return `${s} s left`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min left`;
  return m % 60 ? `${Math.floor(m / 60)} h ${m % 60} min left` : `${m / 60} h left`;
}
function noteUploadPace(u, now = Date.now()) {
  if (!UPLOAD_PACE_PHASES.includes(u.status) || !u.size) { uploadPaces.delete(u.id); return; }
  const sent = u.sent || 0;
  let p = uploadPaces.get(u.id);
  // the copy to HPC3 counts from 0 again, and so does a copy that is retried
  if (!p || p.phase !== u.status || sent < p.samples[p.samples.length - 1][1]) {
    p = { phase: u.status, samples: [[now, sent]] };
    uploadPaces.set(u.id, p);
  } else if (sent > p.samples[p.samples.length - 1][1]) p.samples.push([now, sent]);
  while (p.samples.length > 2 && now - p.samples[0][0] > UPLOAD_PACE_WINDOW_MS) p.samples.shift();
  // A card is repainted when its bytes move. One that stalls is not, and would show its last speed
  // for ever: repaint once a second while anything is being measured.
  if (!uploadPaceTimer) uploadPaceTimer = setInterval(() => {
    if (uploadPaces.size) { renderUploads(); return; }
    clearInterval(uploadPaceTimer); uploadPaceTimer = null;
  }, 1000);
}
// { bps, eta } for a card whose bytes are moving, null otherwise (too early to tell, or stalled).
function uploadPaceOf(u, now = Date.now()) {
  const p = uploadPaces.get(u.id);
  if (!p || p.phase !== u.status || p.samples.length < 2) return null;
  const first = p.samples[0], last = p.samples[p.samples.length - 1];
  if (last[0] - first[0] < 1000 || now - last[0] > UPLOAD_PACE_STALE_MS) return null;
  const bps = ((last[1] - first[1]) * 1000) / (last[0] - first[0]);
  return bps > 0 ? { bps, eta: Math.max(0, u.size - (u.sent || 0)) / bps } : null;
}
// " · 35.2 MB/s · 2 min left", or "" when there is nothing to say.
function uploadPaceText(u) {
  const pace = u && uploadPaceOf(u);
  return pace ? ` · ${fmtUploadSpeed(pace.bps)} · ${fmtUploadEta(pace.eta)}` : "";
}
const uploadPaceTextFor = (id) => uploadPaceText(state.uploads.find((u) => u.id === id));
const uploadDigestCache = new WeakMap();
function hashUploadFile(file, ctrl) {
  if (uploadDigestCache.has(file)) return uploadDigestCache.get(file);
  const promise = new Promise((resolve, reject) => {
    const worker = new Worker("/static/upload-hash-worker.js");
    const abort = () => { worker.terminate(); reject(new DOMException("cancelled", "AbortError")); };
    ctrl.signal.addEventListener("abort", abort, { once: true });
    worker.onmessage = ({ data }) => {
      if (data.type === "done" || data.type === "error") {
        worker.terminate(); ctrl.signal.removeEventListener("abort", abort);
        if (data.type === "error") reject(new Error(data.error)); else resolve(data.sha256);
      }
    };
    worker.onerror = (e) => { worker.terminate(); ctrl.signal.removeEventListener("abort", abort); reject(new Error(e.message)); };
    worker.postMessage(file);
  });
  uploadDigestCache.set(file, promise);
  promise.catch(() => uploadDigestCache.delete(file));
  return promise;
}
// -- resuming over tus ------------------------------------------------------------------------------
// The server's answer behind a tus error. An error raised by one of the hooks below comes back
// wrapped by tus-js-client, with the response only on the error it wraps.
function tusResponseOf(err) {
  return (err && err.originalResponse) || (err && err.causingError && err.causingError.originalResponse) || null;
}
// Asked after every failed request while attempts are left. Everything but a refusal by the server
// is tried again: no answer at all, a 5xx from the proxy, a request the server gave up on because
// no bytes came (408), a chunk that arrived damaged (460), an offset the server corrects (409), an
// upload the server is still busy with or holds for a session not connected yet (423). The
// library's own rule also gives up at once when the browser reports itself offline, which is
// exactly when waiting is right.
function tusShouldRetry(err) {
  const res = tusResponseOf(err);
  const status = res ? res.getStatus() : 0;
  return status !== 507 && (status < 400 || status >= 500 || [408, 409, 423, 460].includes(status));
}
// tus-js-client continues an upload with a HEAD, and takes any answer but 2xx or 423 for "this upload
// cannot be continued": it creates a new one and the file starts again from byte 0. On a 5xx it also
// keeps the old one in storage and so never stores the new one, and a reload continues the abandoned
// upload. A 504 from the proxy did both on prod (2026-10-13). Only the server saying the upload does
// not exist (404) or failed for good (410) may start the file again; any other answer is raised
// here, so tusShouldRetry decides and the stored upload is kept.
function tusResumeGuard(req, res) {
  if (req.getMethod() !== "HEAD") return;
  const status = res.getStatus();
  if ((status >= 200 && status < 300) || [404, 410, 423].includes(status)) return;
  throw new window.tus.DetailedError(`tus: the server could not say where this upload stands (HTTP ${status})`, null, req, res);
}
// Where the server says a stored upload stands. `offset` is -1 when it did not say: a 423 (busy
// with an earlier request, or no session connected) carries the offset it holds, as a 200 does.
async function probeTusUpload(url, request = fetch) {
  try {
    const r = await request(url, { method: "HEAD", headers: tusHeaders() });
    const offset = Number(r.headers.get("Upload-Offset")), length = Number(r.headers.get("Upload-Length"));
    const said = (r.status === 200 || r.status === 423) && Number.isSafeInteger(offset) && offset >= 0;
    return { status: r.status, offset: said ? offset : -1, length: said && Number.isSafeInteger(length) ? length : -1 };
  } catch { return { status: 0, offset: -1, length: -1 }; }
}
const tusGone = (status) => status === 404 || status === 410;
// Of the uploads this browser keeps for one file, the one to continue. There is normally one; two
// tabs that sent the same file leave two, and the first in storage need not be the one further
// along. Each is asked where it stands and the one furthest along is taken. The others are
// forgotten, and their partial bytes on the server deleted, only when every one answered.
async function pickPreviousUpload(previous, storage, request = fetch) {
  if (previous.length < 2) return previous[0] || null;
  const seen = await Promise.all(previous.map(async (up) => ({ up, ...(await probeTusUpload(up.uploadUrl, request)) })));
  const alive = [];
  for (const s of seen) {
    if (tusGone(s.status)) await storage.removeUpload(s.up.urlStorageKey);
    else alive.push(s);
  }
  if (!alive.length) return null;
  const made = (s) => Date.parse(s.up.creationTime) || 0;
  alive.sort((a, b) => b.offset - a.offset || made(b) - made(a));
  if (alive.every((s) => s.offset >= 0)) {
    for (const s of alive.slice(1)) {
      await storage.removeUpload(s.up.urlStorageKey);
      if (s.offset < s.length) request(s.up.uploadUrl, { method: "DELETE", headers: tusHeaders() }).catch(() => {});
    }
  }
  return alive[0].up;
}
// The upload of this file the server holds for the account, for a browser that has none stored: one
// begun on another computer, in a private window, or before the site's data was cleared. The server
// matches the path (or name), size and modification time, and offers the upload only when its first
// chunk is this file's first chunk; no answer, or any other, starts a new upload as before. A file of
// one chunk or less goes in one request and is never left part-way, so it is not looked for.
// urlStorageKey null: tus-js-client stores the upload in this browser once the server says where it stands.
async function findServerUpload(file, fields, request = fetch) {
  if (!(file.size > UPLOAD_CHUNK) || !Number.isSafeInteger(file.lastModified) || !state.connectionId) return null;
  try {
    const head = new Uint8Array(await crypto.subtle.digest("SHA-256", await file.slice(0, UPLOAD_CHUNK).arrayBuffer()));
    const r = await request("/api/tus/find", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, name: file.name, rel_path: fields.rel_path || "",
        size: file.size, last_modified: file.lastModified,
        head_sha256: Array.from(head, (b) => b.toString(16).padStart(2, "0")).join("") }) });
    const found = r.ok ? (await r.json()).upload : null;
    if (!found || !found.url) return null;
    // absolute, as tus-js-client stores the uploads it creates
    return { uploadUrl: new URL(found.url, window.location.href).href, urlStorageKey: null, size: file.size, offset: found.offset };
  } catch { return null; }
}
// The upload to continue for this file: the one this browser stored, else one the server holds.
async function previousUploadFor(upload, file, fields, request = fetch) {
  const stored = await pickPreviousUpload(await upload.findPreviousUploads(), upload.options.urlStorage, request);
  return stored || findServerUpload(file, fields, request);
}

// Send one file over tus; resolves with the gateway's answer for the finished upload
// ({status: "staging", job_id, …} when a copy to HPC3 follows, {status: "complete", …} otherwise).
function tusUpload(file, fields, ctrl, hashPromise, onProgress, onWaitingForHash, onRetrying) {
  if (!window.tus || !window.tus.isSupported) throw new Error("Resumable upload support is unavailable");
  return new Promise((resolveUpload, rejectUpload) => {
    let waiting = false;      // between two attempts: a kick starts the next one now
    let retrying = false;     // a request failed and no chunk has got through since
    const kick = () => {
      if (!waiting || ctrl.signal.aborted) return;
      waiting = false;
      upload.abort().then(() => { if (!ctrl.signal.aborted) upload.start(); }).catch(() => {});
    };
    const moving = () => { if (retrying) { retrying = false; if (onRetrying) onRetrying(false); } };
    const settled = () => { tusKicks.delete(kick); waiting = false; moving(); };
    const resolve = (v) => { settled(); resolveUpload(v); };
    const reject = (e) => { settled(); rejectUpload(e); };
    const upload = new window.tus.Upload(file, {
      endpoint: "/api/tus", chunkSize: UPLOAD_CHUNK, retryDelays: TUS_RETRY_DELAYS,
      removeFingerprintOnSuccess: true,
      fingerprint: async (f) => `tus:${apiKeyOwner()}:${fields.rel_path || f.name}:${f.size}:${f.lastModified}`,
      headers: { "X-Connection-ID": state.connectionId },
      // last_modified lets another browser find this upload again (findServerUpload)
      metadata: { filename: file.name, connection_id: state.connectionId,
        ...(Number.isSafeInteger(file.lastModified) ? { last_modified: String(file.lastModified) } : {}), ...fields },
      onShouldRetry(err) {
        const again = tusShouldRetry(err);
        if (again) {
          waiting = true;
          if (!retrying) { retrying = true; if (onRetrying) onRetrying(true); }
        }
        return again;
      },
      onAfterResponse: tusResumeGuard,
      onChunkComplete: moving,
      async onBeforeRequest(req) {
        waiting = false;                                             // the next attempt has begun
        if (req.getMethod() !== "PATCH") return;
        const offset = Number(req.getHeader("Upload-Offset"));
        if (!Number.isSafeInteger(offset)) throw new Error("Missing tus upload offset");
        const bytes = await file.slice(offset, offset + UPLOAD_CHUNK).arrayBuffer();
        const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
        req.setHeader("Upload-Checksum", `sha256 ${btoa(String.fromCharCode(...digest))}`);
        if (offset + UPLOAD_CHUNK >= file.size && hashPromise) {
          // The last chunk carries the whole-file SHA-256. On a fast link the bytes are out before
          // the worker has read the whole file (20 GiB locally: sent in 112 s, hashed in 287 s), so
          // say what the wait is. A hash that failed must not strand an upload at 100%: every chunk
          // was checked on its own, so the last one goes without the whole-file digest.
          let sha = "", ready = false;
          const done = hashPromise.then((v) => { sha = v; }, (e) => { if (ctrl.signal.aborted) throw e; }).finally(() => { ready = true; });
          await Promise.resolve();
          if (!ready && onWaitingForHash) onWaitingForHash(true);
          try { await done; } finally { if (onWaitingForHash) onWaitingForHash(false); }
          if (sha) req.setHeader("Upload-Final-SHA256", sha);
        }
      },
      onProgress(sent, total) { if (onProgress) onProgress(sent, total); },
      onError: reject,
      async onSuccess() {
        // Every byte is on the server (also when the last chunk's answer was lost and the retry
        // found nothing left to send). Asking what became of the upload must survive a lost
        // connection too: giving up here would offer a Resume that sends the whole file again.
        moving();
        for (let attempt = 0; ; attempt++) {
          try {
            const r = await fetch(`/api/tus/${tusId(upload.url)}`, { headers: { "X-Connection-ID": state.connectionId } });
            if (r.status >= 500 && attempt < TUS_RETRY_DELAYS.length) throw new TypeError(`HTTP ${r.status}`);
            const d = await r.json();
            if (!r.ok) throw new Error(d.error || `Upload result unavailable (${r.status})`);
            resolve(d);
            return;
          } catch (e) {
            if (ctrl.signal.aborted) return;
            if (!(e instanceof TypeError) || attempt >= TUS_RETRY_DELAYS.length) { reject(e); return; }
            await sleep(TUS_RETRY_DELAYS[attempt] || 1000);
          }
        }
      },
    });
    tusKicks.add(kick);
    ctrl.signal.addEventListener("abort", () => {
      upload.abort(true).then(() => reject(new DOMException("cancelled", "AbortError"))).catch((error) => {
        error.cleanupFailed = true;
        reject(error);
      });
    }, { once: true });
    previousUploadFor(upload, file, fields).then((previous) => {
      if (ctrl.signal.aborted) return;
      if (previous) { upload.resumeFromPreviousUpload(previous); dropPendingCardFor(previous.uploadUrl); }
      // a folder's card already says how much of the folder is there
      if (previous && previous.urlStorageKey === null && !fields.rel_path && previous.offset > 0) {
        toast(`Continuing ${file.name} from ${Math.floor((100 * previous.offset) / file.size)}%: the server kept what was sent earlier.`);
      }
      upload.start();
    }).catch(reject);
  });
}

// -- the copy to HPC3: the server pushes every change over the WebSocket ("upload_staging") --------
const stagingJobs = new Map();       // job id -> the latest state the server pushed
const stagingWaiters = new Set();
function wakeStagingWaiters() { for (const wake of [...stagingWaiters]) wake(); }
function onStagingEvent(job) {
  if (!job || !job.id) return;
  stagingJobs.set(job.id, job);
  wakeStagingWaiters();
}
// Resolves at the next staging event, or after `ms`: the socket may be down, and then the caller
// asks the server instead.
function nextStagingEvent(ms) {
  return new Promise((resolve) => {
    const wake = () => { clearTimeout(timer); stagingWaiters.delete(wake); resolve(); };
    const timer = setTimeout(wake, ms);
    stagingWaiters.add(wake);
  });
}
async function stagingJob(jobId) {
  const r = await fetch(`/api/upload/staging?connection_id=${encodeURIComponent(state.connectionId)}&job_id=${encodeURIComponent(jobId)}`);
  const d = await r.json().catch(() => ({}));
  return d.job || null;
}
// Follow one background copy until it settles; returns an upload-shaped answer.
async function finishStaging(d, id, name, size, signal) {
  const onAbort = () => { postJson("/api/upload/staging/cancel", { job_id: d.job_id }).catch(() => {}); wakeStagingWaiters(); };
  if (signal) signal.addEventListener("abort", onAbort, { once: true });
  try {
    let job = await stagingJob(d.job_id).catch(() => null);
    for (;;) {
      if (signal && signal.aborted) throw new DOMException("cancelled", "AbortError");
      if (job) {
        if (job.state === "done") return { ...d, status: "uploaded", name: job.name, path: job.remote_path,
          size: job.size, peek: job.peek || d.peek, unpacked: job.unpacked, unpack_error: job.unpack_error };
        if (job.state === "failed" || job.state === "cancelled") return { ...d, status: job.state, error: job.error };
        const status = job.state === "verifying" ? "verifying" : job.state === "unpacking" ? "unpacking" : "staging";
        // "queued" with an error is a copy waiting for its retry, or for an HPC3 session: say so
        upsertUpload({ id, status, sent: job.sent || 0, size: job.size || size, dest: job.dest,
          note: job.state === "queued" && job.error ? `Waiting — ${job.error}` : "" });
        const w = stagingWords(job);
        setUploadProgress(id, status === "verifying" ? `${w.verify} ${name}…`
          : status === "unpacking" ? `${w.unpack}: ${name}…`
          : job.dest === "server" ? `Checking ${name} on the server…`
          : `Copying ${name} to HPC3… ${job.size ? Math.floor((100 * (job.sent || 0)) / job.size) : 0}%${uploadPaceTextFor(id)}`);
      }
      const seen = stagingJobs.get(d.job_id);
      await nextStagingEvent(STAGING_POLL_MS);
      const now = stagingJobs.get(d.job_id);
      job = now && now !== seen ? now : await stagingJob(d.job_id).catch(() => null);
    }
  } finally { if (signal) signal.removeEventListener("abort", onAbort); }
}
// Each file's row during the copy to HPC3 (or its check on the server): a file whose copy failed says
// so (until a retry copies it); once the server has recorded the folder, every copied file is uploaded.
const COPY_FAILED = new Set([stagingWords({ dest: "hpc3" }).failed, stagingWords({ dest: "server" }).failed]);
function markFolderCopies(id, f) {
  const card = state.uploads.find((u) => u.id === id);
  if (!card || !card.fileProgress) return;
  const failed = new Set(f.failed_files || []);
  for (const row of card.fileProgress) {
    const rel = String(row.name || "").split("/").slice(1).join("/");
    if (failed.has(rel)) { row.status = "error"; row.error = stagingWords(f).failed; }
    else if (row.status === "copying" || (row.status === "error" && COPY_FAILED.has(row.error))) {
      row.status = f.recorded ? "done" : "copying"; row.error = "";
    }
  }
}
// The server records a folder once every file of it is verified on HPC3; follow that.
async function finishFolderStaging(folder, d, id, signal) {
  const onAbort = () => { postJson("/api/upload/staging/cancel", { folder }).catch(() => {}); wakeStagingWaiters(); };
  if (signal) signal.addEventListener("abort", onAbort, { once: true });
  let first = true;
  try {
    for (;;) {
      if (signal && signal.aborted) throw new DOMException("cancelled", "AbortError");
      const asked = Date.now();
      // The copies run on the server whether or not this page can reach it: an unanswered question
      // is asked again, it does not end the card (the single-file follower does the same).
      const r = await fetch(`/api/upload/staging?connection_id=${encodeURIComponent(state.connectionId)}&folder=${encodeURIComponent(folder)}`).catch(() => null);
      const f = r && r.ok ? ((await r.json().catch(() => ({}))).folder || {}) : null;
      if (f) {
        markFolderCopies(id, f);
        if (f.recorded) return { ...d, status: "registered", name: d.name || f.original_name || folder, path: f.remote_root || d.path, size: f.size, count: f.count, kept: f.kept_count || 0 };
        if (f.pending === 0 && f.failed > 0) throw new Error(`${f.failed} of ${f.count} files ${stagingWords(f).missing}`);
        // what is on HPC3 before this page first looks (copied meanwhile, or there from an earlier upload) is not speed
        if (first) { uploadPaces.delete(id); first = false; }
        upsertUpload({ id, status: "staging", dest: f.dest, filesDone: f.done || 0, filesTotal: f.count || 0, filesFailed: f.failed || 0, sent: f.sent || 0, size: f.size || 0 });
        setUploadProgress(id, f.dest === "server"
          ? `Checking ${shownFolder(folder, f.original_name)}/ on the server — ${f.done || 0}/${f.count || "?"} files`
          : `Copying ${shownFolder(folder, f.original_name)}/ to HPC3 — ${f.done || 0}/${f.count || "?"} files${uploadPaceTextFor(id)}`);
      }
      await nextStagingEvent(STAGING_POLL_MS);
      // every file of a big folder sends events: ask the server at most once a second
      const early = 1000 - (Date.now() - asked);
      if (early > 0) await sleep(early);
    }
  } finally { if (signal) signal.removeEventListener("abort", onAbort); }
}

// -- one file ----------------------------------------------------------------------------------------
// All bytes of a file are on the gateway: follow its copy to HPC3, then show the dataset.
async function settleUpload(id, d, name, size, ctrl, attach) {
  if (d.status === "staging") {
    upsertUpload({ id, jobId: d.job_id, status: "staging", sent: 0, error: "" });
    d = await finishStaging(d, id, name, size, ctrl.signal);
  }
  if (d.status === "failed" || d.status === "cancelled") throw new Error(d.error || `upload ${d.status}`);
  upsertUpload({ id, sent: size, status: "done", note: "" });
  const unpacked = d.unpacked;
  // Carry the upload-time content skim (the deterministic peek gist) onto the chip so the user sees
  // "what this is" per attached file, not just the name. An unpacked archive is its folder. It joins
  // the chat the upload was started from, which may not be the one on screen any more.
  const chat = uploadChat(id);
  if (attach) addDataset({ name: unpacked ? `${unpacked.path.replace(/\/$/, "").split("/").pop()}/` : d.name,
    path: unpacked ? unpacked.path : d.path, kind: unpacked ? "folder" : "file", gist: (d.peek && d.peek.gist) || "" }, chat);
  const where = attach && chat && String(chat) !== String(state.activeId) ? ` — attached in “${chatTitle(chat)}”` : "";
  if (d.unpack_error) toast(`Uploaded ${d.name}, but it was not unpacked: ${d.unpack_error}`);
  else toast(`Uploaded ${d.name || name} (${fmtUploadSize(d.size || size)})${where}`);   // d.name may be uniquified
  loadDatasets();                                                  // reflect the new file in the tab
  setTimeout(() => removeUpload(id), 4000);                        // clear the "Done" card after a beat
}
function failUpload(id, err, name, ctrl) {
  if (err.cleanupFailed) {
    upsertUpload({ id, status: "error", error: `Cancellation cleanup failed: ${err.message}`, note: "" });
    toast(`Cancellation cleanup failed for ${name}: ${err.message}`);
    return;
  }
  if ((ctrl && ctrl.signal.aborted) || err.name === "AbortError") {      // the user cancelled: not an error
    upsertUpload({ id, status: "cancelled", note: "" });
    toast(`Cancelled ${name}`);
    setTimeout(() => removeUpload(id), 4000);
    return;
  }
  const u = state.uploads.find((x) => x.id === id) || {};
  const pct = u.size ? Math.floor((100 * (u.sent || 0)) / u.size) : 0;
  // A card with a job is past the upload: what failed is the copy, and "Retry copy" is its button.
  const uploading = !(u.jobId || u.serverFolder);
  const f = uploadFailure(err, uploading && pct ? ` at ${pct}%` : "",
    uploading ? "Resume continues from there." : "Try again when the connection is back.");
  upsertUpload({ id, status: "error", note: "", error: f.text, detail: f.detail, lost: f.lost });
  reportUploadError(id, name, err);
  toast(`${name}: ${f.text}`);
}
// A failure in the browser (the connection dropped, the file could not be read) is one the server
// never sees by itself: tell it, so its upload log does not simply stop.
function reportUploadError(id, name, err) {
  const u = state.uploads.find((x) => x.id === id) || {};
  postJson("/api/upload/report", { name, kind: u.kind || "file", size: u.size || 0, sent: u.sent || 0,
    error: (err && err.message) || "upload failed" }).catch(() => {});
}
async function uploadDataset(file) {
  if (!file) return;
  if (!state.connectionId) { toast("Connect to HPC3 first."); return; }
  // Stable per-file id, so choosing the same file again (Resume) reuses its card.
  const id = "u" + Math.abs(hashStr(`${file.name}:${file.size}:${file.lastModified}`)).toString(36);
  const ctrl = new AbortController();
  // An archive is asked about once: a Resume, by hand or by itself, keeps the first answer.
  const before = state.uploads.find((u) => u.id === id && u.status === "error");
  let fields = before && before.fields;
  state.uploads = state.uploads.filter((u) => !(u.status === "pending" && u.kind === "file" && u.name === file.name && u.size === file.size));
  upsertUpload({ id, kind: "file", name: file.name, size: file.size, sent: (before && before.sent) || 0, status: "queued", file,
    error: "", detail: "", lost: false, retrying: false, note: "", jobId: "", ctrl,
    chat: (before && before.chat) || state.activeId });   // the chat this upload attaches to when done
  setUploadProgress(id, `Uploading ${file.name}…`);
  try {
    if (!fields) {
      fields = ARCHIVE_RE.test(file.name) && window.confirm(`Unpack ${file.name} after verified transfer to HPC3?`)
        ? { unpack: "1" } : {};
      upsertUpload({ id, fields });
    }
    await uploadSlot();
    let d;
    try {
      if (ctrl.signal.aborted) throw new DOMException("cancelled", "AbortError");
      const hashPromise = hashUploadFile(file, ctrl);
      hashPromise.catch(() => {}); // cancellation before the final PATCH still has a handled promise
      upsertUpload({ id, status: "uploading" });
      d = await tusUpload(file, { ...fields, ...uploadChatField(id) }, ctrl, hashPromise, (sent, total) => {
        upsertUpload({ id, sent, size: total, status: "uploading" });
        setUploadProgress(id, `Uploading ${file.name}… ${total ? Math.floor((100 * sent) / total) : 0}%${uploadPaceTextFor(id)}`);
      }, (waiting) => {
        upsertUpload({ id, note: waiting ? "Finishing the checksum of the whole file…" : "" });
        if (waiting) setUploadProgress(id, `Finishing the checksum of ${file.name}…`);
      }, (retrying) => upsertUpload({ id, retrying }));
    } finally { releaseUploadSlot(); }
    await settleUpload(id, d, file.name, file.size, ctrl, true);
  } catch (err) { failUpload(id, err, file.name, ctrl); }
  finally { setUploadProgress(id, null); }
}

// -- the cards ---------------------------------------------------------------------------------------
function upsertUpload(rec) {
  const i = state.uploads.findIndex((u) => u.id === rec.id);
  if (i >= 0) state.uploads[i] = { ...state.uploads[i], ...rec };
  else state.uploads.unshift(rec);
  noteUploadPace(state.uploads[i >= 0 ? i : 0]);
  renderUploads();
}
function removeUpload(id) { state.uploads = state.uploads.filter((u) => u.id !== id); uploadPaces.delete(id); uploadLines.delete(id); renderUploads(); }
// An "Interrupted" card stands for one upload on the server. Once that upload continues, the card of
// the file being sent says how it goes. Matched by upload, not by name: a card from the server's list
// may carry the server's cleaned name ("my data.bam" is "mydata.bam" there).
function dropPendingCardFor(url) {
  const id = tusId(url);
  for (const u of state.uploads.filter((x) => x.status === "pending" && x.tusUrl && tusId(x.tusUrl) === id)) removeUpload(u.id);
}
function resumeUpload(id) {
  const u = state.uploads.find((x) => x.id === id);
  if (u && u.kind === "folder" && u.files) uploadFolder(u.files);    // only the missing files are sent
  else if (u && u.file) uploadDataset(u.file);
}
// Cancel: abort whatever is in flight for this card; the code driving it finalises the card.
function cancelUpload(id) { const u = state.uploads.find((x) => x.id === id); if (u && u.ctrl) u.ctrl.abort(); }
// The composer's Cancel: the uploads of the chat on screen, never another chat's.
function cancelActiveUploads() {
  chatUploads(state.activeId).forEach((u) => cancelUpload(u.id));
}
// The copy to HPC3 failed (the file is still on the server): run it again, without uploading again.
async function retryUpload(id) {
  const u = state.uploads.find((x) => x.id === id);
  if (!u || !(u.jobId || u.serverFolder)) return;
  const ctrl = new AbortController();
  try {
    const r = await postJson("/api/upload/staging/retry", u.serverFolder ? { folder: u.serverFolder } : { job_id: u.jobId });
    const d = await r.json().catch(() => ({}));
    if (!r.ok || d.status !== "retrying") { toast(d.error || "Nothing left to retry: upload it again."); return; }
    upsertUpload({ id, status: "staging", error: "", note: "", ctrl });
    if (u.serverFolder) await followFolder(id, u.serverFolder, {}, ctrl, u.attach !== false);
    else await settleUpload(id, { status: "staging", job_id: u.jobId, name: u.name, size: u.size }, u.name, u.size, ctrl, u.attach !== false);
  } catch (err) { failUpload(id, err, u.name, ctrl); }
  finally { setUploadProgress(id, null); }
}
// Give a failed upload up: the server drops the job and what it had kept for a retry.
async function dismissUpload(id) {
  const u = state.uploads.find((x) => x.id === id);
  if (!u) return;
  if (u.jobId || u.serverFolder) {
    await postJson("/api/upload/staging/dismiss", u.serverFolder ? { folder: u.serverFolder } : { job_id: u.jobId }).catch(() => {});
  }
  removeUpload(id);
}
// Forget an upload the browser never finished: the gateway deletes the bytes it holds.
async function discardUpload(id) {
  const u = state.uploads.find((x) => x.id === id);
  if (!u) return;
  try {
    for (const url of [u.tusUrl, ...(u.tusUrls || [])].filter(Boolean)) {   // tusUrls: a folder's part-way files
      await fetch(url, { method: "DELETE", headers: tusHeaders() }).catch(() => {});
    }
    if (u.tusKey && window.tus) await window.tus.defaultOptions.urlStorage.removeUpload(u.tusKey);
    if (u.folderKey) { localStorage.removeItem(u.folderKey); localStorage.removeItem(`${u.folderKey}:completed`); }
  } catch { /* the card goes either way */ }
  removeUpload(id);
}
function uploadAction(act, id) {
  if (act === "cancel") cancelUpload(id);
  else if (act === "resume") resumeUpload(id);
  else if (act === "retry") retryUpload(id);
  else if (act === "dismiss") dismissUpload(id);
  else if (act === "discard") discardUpload(id);
  else if (act === "pickfile") $("datasetFile").click();
  else if (act === "pickdir") $("datasetDir").click();
  else if (act === "openchat") openChat(uploadChat(id));
}
function uploadFileCounts(u) {
  const rows = u.serverFolder ? [] : (u.fileProgress || []);
  if (rows.length && u.status === "staging") {
    // the copy to HPC3: the server counts which files are verified there
    const done = u.filesDone || 0, failed = u.filesFailed || 0;
    return { done, failed, cancelled: 0, remaining: Math.max(0, (u.filesTotal || rows.length) - done - failed) };
  }
  const done = rows.length ? rows.filter((f) => f.status === "done").length : (u.filesDone || 0);
  const failed = rows.length ? rows.filter((f) => f.status === "error").length : (u.filesFailed || 0);
  const cancelled = rows.filter((f) => f.status === "cancelled").length;
  return { done, failed, cancelled, remaining: Math.max(0, (u.filesTotal || rows.length) - done - failed - cancelled) };
}
// What the background step after an upload is called: with a data root the upload is kept on the
// server (job.dest "server") and the queue only checks it there (and unpacks an archive); otherwise
// it is copied to HPC3. A declaration, so the code above it can use it too.
function stagingWords(x) {
  return x && x.dest === "server"
    ? { copying: "Checking", verify: "Checking", unpack: "Unpacking on the server", failed: "the check on the server failed",
        missing: "failed their check on the server" }
    : { copying: "Copying to HPC3", verify: "Verifying on HPC3", unpack: "Unpacking on HPC3", failed: "the copy to HPC3 failed",
        missing: "did not reach HPC3" };
}
function uploadFileRows(u) {
  return (u.fileProgress || []).map((f) => {
    const pct = f.size ? Math.min(100, Math.round(f.sent / f.size * 100)) : f.status === "done" ? 100 : 0;
    const label = { queued: "Waiting", uploading: "Uploading", verifying: "Verifying", copying: stagingWords(u).copying, done: "Uploaded", error: "Failed", cancelled: "Cancelled" }[f.status] || f.status;
    return `<div class="upload-file ${f.status}"><span class="upload-file-name" title="${escapeHtml(f.name)}">${escapeHtml(f.name)}</span>` +
      `<span>${fmtUploadSize(f.sent)} / ${fmtUploadSize(f.size)} · ${pct}%</span>` +
      `<span title="${escapeHtml(f.error || "")}">${escapeHtml(label)}${f.error ? `: ${escapeHtml(f.error)}` : ""}</span></div>`;
  }).join("");
}
function uploadCardView(u) {
  const pct = u.size ? Math.min(100, Math.round(((u.sent || 0) / u.size) * 100)) : u.status === "done" ? 100 : 0;
  const counts = uploadFileCounts(u);
  const files = u.filesTotal ? `${counts.done} completed · ${counts.remaining} remaining · ${counts.failed} failed${counts.cancelled ? ` · ${counts.cancelled} cancelled` : ""}` : "";
  const pace = uploadPaceText(u);
  let stat;
  if (u.status === "done") stat = "Done";
  else if (u.status === "cancelled") stat = "Cancelled";
  else if (u.status === "queued") stat = "Waiting for an upload slot…";
  else if (u.status === "staging") stat = u.note || (u.dest === "server" ? `Checking on the server… ${files}` : `Copying to HPC3… ${files || `${pct}%`}${pace}`);
  else if (u.status === "verifying") stat = `${stagingWords(u).verify}…`;
  else if (u.status === "unpacking") stat = `${stagingWords(u).unpack}…`;
  else if (u.status === "error") stat = `${u.lost ? "Interrupted" : "Failed"} — ${u.error || "interrupted"}`;
  else if (u.status === "pending") stat = u.note || "Interrupted — choose it again to continue";
  else if (u.retrying) stat = `Connection lost at ${files ? files + ", " : ""}${pct}% — trying again…`;
  else stat = u.note || `${files ? files + " · " : ""}${pct}% · ${fmtUploadSize(u.sent)} / ${fmtUploadSize(u.size)}${pace}`;
  const buttons = [];
  if (UPLOAD_ACTIVE.includes(u.status)) buttons.push(["cancel", "Cancel"]);
  else if (u.status === "error") {
    if (u.jobId || u.serverFolder) buttons.push(["retry", u.dest === "server" ? "Retry check" : "Retry copy"]);
    else if (u.kind === "folder") buttons.push(u.files ? ["resume", "Retry failed files"] : ["pickdir", "Choose the folder again"]);
    else if (u.file) buttons.push(["resume", "Resume"]);
    buttons.push(["dismiss", "Dismiss"]);
  } else if (u.status === "pending") {
    buttons.push(u.kind === "folder" ? ["pickdir", "Choose folder"] : ["pickfile", "Choose file"], ["discard", "Discard"]);
  }
  const icon = u.status === "error" ? "error" : u.status === "cancelled" ? "cancel"
    : u.status === "done" ? "check_circle" : u.status === "pending" ? "pause_circle"
    : u.kind === "folder" ? "drive_folder_upload" : "upload_file";
  // the upload library's own wording, for whoever has to debug it
  const tip = u.status === "error" && u.detail && u.detail !== u.error ? `${stat}\n\n${u.detail}` : stat;
  if (u.status !== "uploading") stat += ` · ${pct}% · ${fmtUploadSize(u.sent)} / ${fmtUploadSize(u.size)}`;
  if (files && u.status !== "uploading" && u.status !== "staging") stat += ` · ${files}`;
  return { stat, pct, buttons, icon, tip };
}
// Cards are updated in place. Rebuilding them on every progress event replaced the buttons several
// times a second, and a click whose press and release fell on two different buttons was lost.
function renderUploads() {
  const host = $("uploadsPanel");
  if (!host) return;
  host.classList.toggle("hidden", !state.uploads.length);
  const have = new Map([...host.children].map((el) => [el.dataset.uploadId, el]));
  const wanted = new Set(state.uploads.map((u) => u.id));
  for (const [id, el] of have) if (!wanted.has(id)) el.remove();
  let prev = null;
  for (const u of state.uploads) {
    const v = uploadCardView(u);
    const shape = `${u.status}|${v.icon}|${v.buttons.map((b) => b[0]).join(",")}`;
    let el = have.get(u.id);
    if (!el || el.dataset.shape !== shape) {
      const fresh = document.createElement("div");
      fresh.className = `upload-card ${u.status}`;
      fresh.dataset.uploadId = u.id;
      fresh.dataset.shape = shape;
      fresh.innerHTML = `<div class="upload-row"><span class="material-symbols-outlined msym">${v.icon}</span>` +
        `<span class="upload-name"></span><button type="button" class="upload-chat hidden" data-act="openchat"></button>` +
        `<span class="upload-stat"></span>` +
        v.buttons.map(([act, label]) => `<button type="button" class="ghost small" data-act="${act}">${escapeHtml(label)}</button>`).join("") +
        `</div><div class="meter"><div class="meter-fill"></div></div>` +
        `<details class="upload-files"><summary>File progress</summary><div class="upload-file-list"></div></details>`;
      if (el) {
        fresh.querySelector(".upload-files").open = !!el.querySelector(".upload-files")?.open;
        el.replaceWith(fresh);
      }
      el = fresh;
    }
    const nameEl = el.querySelector(".upload-name");
    nameEl.textContent = u.name; nameEl.title = u.name;
    const from = chatLabel(u.chat);
    const chatEl = el.querySelector(".upload-chat");
    chatEl.classList.toggle("hidden", !from);
    chatEl.disabled = !from || !from.open;
    chatEl.textContent = from ? from.text : "";
    chatEl.title = from && from.open ? "Open the chat this upload was started from" : "";
    const statEl = el.querySelector(".upload-stat");
    statEl.textContent = v.stat; statEl.title = v.tip;
    const fill = el.querySelector(".meter-fill");
    fill.style.width = `${v.pct}%`;
    fill.classList.toggle("mem", u.status === "done");
    const details = el.querySelector(".upload-files");
    details.hidden = !(u.fileProgress && u.fileProgress.length);
    if (details.open) {
      const list = details.querySelector(".upload-file-list");
      const html = uploadFileRows(u);
      if (list.innerHTML !== html) list.innerHTML = html;
    }
    details.ontoggle = () => { if (details.open) details.querySelector(".upload-file-list").innerHTML = uploadFileRows(state.uploads.find((x) => x.id === u.id) || u); };
    const at = prev ? prev.nextElementSibling : host.firstElementChild;
    if (at !== el) host.insertBefore(el, at);
    prev = el;
  }
  renderUploadLine();
}

// -- a folder: one card ------------------------------------------------------------------------------
// Every file of the folder is registered: follow the copies to HPC3, then show the one dataset.
async function followFolder(id, folder, d, ctrl, attach, onRecorded) {
  upsertUpload({ id, serverFolder: folder, status: "staging", error: "", note: "" });
  d = await finishFolderStaging(folder, d, id, ctrl.signal);
  const card = state.uploads.find((u) => u.id === id) || {};
  upsertUpload({ id, status: "done", sent: card.size || d.size || 0, filesDone: d.count, filesTotal: d.count });
  // recorded: the browser's resume notes for this folder have nothing left to resume
  if (card.folderKey) { try { localStorage.removeItem(card.folderKey); localStorage.removeItem(`${card.folderKey}:completed`); } catch { /* private mode */ } }
  if (attach) addDataset({ name: `${d.name}/`, path: d.path, kind: "folder" }, uploadChat(id));
  if (onRecorded) onRecorded();
  toast(folderDoneText(d));
  loadDatasets();
  setTimeout(() => removeUpload(id), 4000);
}
// `kept` files of the folder were already uploaded when this upload started (see uploadFolder).
function folderDoneText(d) {
  const all = `${d.count} files, ${fmtUploadSize(d.size)}`;
  if (d.already || (d.kept && d.kept >= d.count)) return `${d.name}/ was already uploaded (${all}): nothing was sent again`;
  if (d.kept) return `Added ${d.count - d.kept} files to ${d.name}/: now ${all}`;
  return `Uploaded folder ${d.name}/ (${all})`;
}
// Upload a WHOLE folder (webkitdirectory) — every file keeps its nested relative path, so
// a multi-level folder round-trips intact. Registered as ONE dataset entry; all uploads
// this session stay reachable to the agent (server exposes the uploads tree to run_code).
// The folder's name as the user knows it: the server's own name ("data_2"), unless it is a managed
// storage id, which says nothing to the user.
const STORAGE_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function shownFolder(serverName, userName) {
  return serverName && !STORAGE_ID.test(serverName) ? serverName : (userName || serverName || "folder");
}
async function uploadFolder(fileList) {
  const files = Array.from(fileList || []).filter((f) => f && f.size >= 0)
    .sort((a, b) => {
      const left = a.webkitRelativePath || a.name, right = b.webkitRelativePath || b.name;
      return left < right ? -1 : left > right ? 1 : 0;
    });
  if (!files.length) return;
  if (!state.connectionId) { toast("Connect to HPC3 first."); return; }
  const folder0 = (files[0].webkitRelativePath || files[0].name).split("/")[0];
  const signature = hashStr(files.map((f) => `${f.webkitRelativePath}:${f.size}:${f.lastModified}`).join("|"));
  const folderKey = `upload-folder:${apiKeyOwner()}:${folder0}:${files.length}:${signature}`;
  const bitsKey = `${folderKey}:completed`;
  const doneBits = new Uint8Array(Math.ceil(files.length / 8));
  const isDone = (i) => !!(doneBits[i >> 3] & (1 << (i & 7)));
  try {
    const saved = localStorage.getItem(bitsKey);
    if (saved) {
      const bytes = Uint8Array.from(atob(saved), (c) => c.charCodeAt(0));
      if (bytes.length === doneBits.length) doneBits.set(bytes);
    }
  } catch { /* An unavailable browser store must not fail a completed transfer. */ }
  const id = "F" + Math.abs(hashStr(folderKey)).toString(36);       // the same id a restored "interrupted" card has
  const total = files.reduce((n, f) => n + f.size, 0);
  const ctrl = new AbortController();
  // `files` stays on the card: while the page is open, a Resume does not need the folder chosen again
  const earlier = state.uploads.find((u) => u.id === id);
  if (earlier && earlier.fileProgress) earlier.fileProgress.forEach((f, i) => {
    if (f.status === "done") doneBits[i >> 3] |= 1 << (i & 7);
  });
  const fileProgress = files.map((f, i) => ({ name: f.webkitRelativePath || f.name, size: f.size,
    sent: isDone(i) ? f.size : 0, status: isDone(i) ? "done" : "queued", error: "" }));
  upsertUpload({ id, kind: "folder", name: `${folder0}/`, size: total, sent: 0, filesDone: 0, filesTotal: files.length, fileProgress,
    status: "uploading", error: "", detail: "", lost: false, retrying: false, note: "", jobId: "", serverFolder: "", folderKey, files, ctrl,
    chat: (earlier && earlier.chat) || state.activeId });
  setUploadProgress(id, `Preparing ${folder0}/…`);
  let folder = folder0, failedRel = "";
  try {
    // Ask the server which folder this goes to. It is told what the folder holds, so it recognises
    // one that is already uploaded, in part or whole (an upload that stopped, in this browser or
    // another, or the same folder chosen again): the answer is then that folder, and `have` lists
    // the files, by position, that are there and are not sent again. A different folder of the
    // same name lands in "name_2/" (or, with managed storage, gets a storage id of its own) instead of
    // merging into / overwriting the first. A folder this browser started before is named in the
    // request: with managed storage the server checks it again (a browser note can outlive the
    // server's 24-hour upload expiry) and only the files it still holds are skipped; without, it is
    // taken as it is ("resume") and the browser's own record of finished files stands.
    let savedFolder = (earlier && earlier.reservedFolder) || "", kept = 0;
    try { savedFolder = localStorage.getItem(folderKey) || savedFolder; } catch { /* private mode */ }
    const rr = await postJson("/api/upload/reserve-folder", { name: folder0, folder: savedFolder, ...uploadChatField(id),
      files: files.map((f) => [(f.webkitRelativePath || `${folder0}/${f.name}`).split("/").slice(1).join("/"), f.size]) });
    const rd = await rr.json().catch(() => ({}));
    if (!rr.ok || !rd.folder) throw new Error(rd.error || "folder reservation failed");
    folder = rd.folder;
    if (!rd.resume) {
      doneBits.fill(0);
      fileProgress.forEach((f) => { f.sent = 0; f.status = "queued"; f.error = ""; });
      for (const i of rd.have || []) {
        if (Number.isInteger(i) && i >= 0 && i < files.length && !isDone(i)) { doneBits[i >> 3] |= 1 << (i & 7); kept++; }
      }
    }
    try {
      localStorage.setItem(folderKey, folder);
      localStorage.setItem(bitsKey, btoa(String.fromCharCode(...doneBits)));
    } catch { /* upload still works this session */ }
    // (all of them there: the closing message says so, see folderDoneText)
    if (kept && kept < files.length) toast(`${folder0}/ is already uploaded in part: ${kept} of ${files.length} files are there, sending the other ${files.length - kept}`);
    upsertUpload({ id, reservedFolder: folder });
    // the server's card for this folder's part-way files (restoreServerCards): this card continues them
    for (const u of state.uploads.filter((x) => x.status === "pending" && x.partsFolder === folder)) removeUpload(u.id);
    let done = 0, doneBytes = 0, nextFile = 0, stopped = false, firstError = null, storageWarned = false, painted = 0;
    let retryingFiles = 0;
    files.forEach((f, i) => { if (isDone(i)) { done++; doneBytes += f.size; fileProgress[i].sent = f.size; fileProgress[i].status = "done"; } });
    uploadPaces.delete(id);             // files that are already there are not speed: measure from here
    const inFlight = new Map();         // file index -> bytes of it sent so far
    const controllers = new Set();
    ctrl.signal.addEventListener("abort", () => { stopped = true; for (const c of controllers) c.abort(); }, { once: true });
    const paint = (force) => {
      if (!force && Date.now() - painted < 250) return;
      painted = Date.now();
      let sent = doneBytes;
      for (const n of inFlight.values()) sent += n;
      upsertUpload({ id, name: `${shownFolder(folder, folder0)}/`, sent, filesDone: done, fileProgress, status: "uploading" });
      setUploadProgress(id, `Uploading ${shownFolder(folder, folder0)}/ — ${done}/${files.length} files${uploadPaceTextFor(id)}`);
    };
    paint(true);
    async function folderWorker() {
      while (!stopped && nextFile < files.length) {
        const index = nextFile++;
        const f = files[index];
        if (isDone(index)) continue;
        const parts = (f.webkitRelativePath || `${folder0}/${f.name}`).split("/");
        parts[0] = folder;
        const rel = parts.join("/");
        const fctrl = new AbortController();
        controllers.add(fctrl);
        try {
          await uploadSlot();
          let d;
          try {
            if (stopped || fctrl.signal.aborted) throw new DOMException("cancelled", "AbortError");
            fileProgress[index].status = "uploading";
            const hashPromise = hashUploadFile(f, fctrl);
            hashPromise.catch(() => {});
            d = await tusUpload(f, { folder, rel_path: rel, ...uploadChatField(id) }, fctrl, hashPromise, (sent) => { fileProgress[index].sent = sent; inFlight.set(index, sent); paint(false); },
              (waiting) => { fileProgress[index].status = waiting ? "verifying" : "uploading"; paint(true); }, (retrying) => {
                retryingFiles += retrying ? 1 : -1;
                upsertUpload({ id, retrying: retryingFiles > 0 });
              });
          } finally { releaseUploadSlot(); }
          if (d.status === "failed" || d.status === "cancelled") throw new Error(d.error || `upload ${d.status}`);
          doneBits[index >> 3] |= 1 << (index & 7);
          try { localStorage.setItem(bitsKey, btoa(String.fromCharCode(...doneBits))); }
          catch { storageWarned = true; }
          inFlight.delete(index);
          done++; doneBytes += f.size;
          fileProgress[index].sent = f.size;
          fileProgress[index].status = d.status === "staging" ? "copying" : "done";
          paint(true);
        } catch (error) {
          fileProgress[index].status = ctrl.signal.aborted ? "cancelled" : "error";
          fileProgress[index].error = ctrl.signal.aborted ? "" : (error.message || "Upload failed");
          if (!firstError && !stopped) { firstError = error; failedRel = rel; }
          // A failed file keeps its resumable partial. Other files finish independently.
          paint(true);
        } finally { controllers.delete(fctrl); }
      }
    }
    await Promise.all(Array.from({ length: Math.min(TUS_CONCURRENCY, files.length) }, () => folderWorker()));
    if (ctrl.signal.aborted) throw new DOMException("cancelled", "AbortError");
    if (firstError) throw firstError;
    const reg = await postJson("/api/upload/register-folder", { folder, count: files.length, ...uploadChatField(id) });
    const d = await reg.json().catch(() => ({}));
    if (!reg.ok) throw new Error(d.error || "folder registration failed");
    const forget = () => {
      try { localStorage.removeItem(folderKey); localStorage.removeItem(bitsKey); } catch { /* no effect on uploaded data */ }
      if (storageWarned) toast("Upload completed; browser storage could not save resume progress.");
    };
    if (d.status === "staging") {
      upsertUpload({ id, filesDone: 0, sent: 0 });                  // now counting copies to HPC3, from none
      await followFolder(id, folder, d, ctrl, true, forget);
    }
    else {                                                           // recorded already: nothing left to copy to HPC3
      upsertUpload({ id, status: "done", sent: total, filesDone: files.length });
      addDataset({ name: `${d.name}/`, path: d.path, kind: "folder" }, uploadChat(id));
      forget();
      toast(folderDoneText({ ...d, kept: d.kept || kept }));
      loadDatasets();
      setTimeout(() => removeUpload(id), 4000);
    }
  } catch (err) {
    if (ctrl.signal.aborted || err.name === "AbortError") {
      upsertUpload({ id, status: "cancelled", note: "" });
      fileProgress.forEach((f) => { if (f.status !== "done") f.status = "cancelled"; });
      try {
        const response = await postJson("/api/upload/staging/cancel", { folder, discard_upload: true });
        if (!response.ok) throw new Error("Server could not remove temporary files");
        localStorage.removeItem(folderKey); localStorage.removeItem(bitsKey);
        toast(`Cancelled ${folder0}/`);
      } catch (cleanupError) {
        upsertUpload({ id, status: "error", error: `Cancellation cleanup failed: ${cleanupError.message}` });
        toast(`Cancellation cleanup failed for ${folder0}/: ${cleanupError.message}`);
        return;
      }
      setTimeout(() => removeUpload(id), 4000);
    } else {
      const f = uploadFailure(err, "", "Resume uploads only the files that are still missing.");
      const text = failedRel && !f.lost ? `${failedRel}: ${f.text}` : f.text;   // a refusal names its file
      upsertUpload({ id, status: "error", note: "", error: text, detail: f.detail, lost: f.lost });
      reportUploadError(id, `${folder0}/`, err);
      toast(`${folder0}/: ${text}`);
    }
  } finally { setUploadProgress(id, null); }
}

// -- after a page reload -----------------------------------------------------------------------------
// Copies to HPC3 run on the server whether or not a page is open; uploads the browser had not finished
// wait in its storage, and the server lists those of any browser. Bring them back as cards, once per
// connection.
let uploadsRestoredFor = "";
async function restoreUploads() {
  const cid = state.connectionId;
  if (!cid || uploadsRestoredFor === cid) return;
  uploadsRestoredFor = cid;
  await Promise.all([restoreStagingCards().catch(() => {}), restorePendingCards().catch(() => {})]);
}
async function restoreStagingCards() {
  const r = await fetch(`/api/upload/staging?connection_id=${encodeURIComponent(state.connectionId)}&summary=1`);
  if (!r.ok) return;
  const d = await r.json().catch(() => ({}));
  const tracked = new Set(state.uploads.flatMap((u) => [u.jobId, u.serverFolder && `folder:${u.serverFolder}`]).filter(Boolean));
  for (const job of d.jobs || []) {
    if (tracked.has(job.id) || job.state === "done" || job.state === "cancelled") continue;
    const id = `j${job.id}`;
    // the chat it was started from gets its progress back, and the data once it is there
    const chat = job.conversation_id || null;
    const base = { id, kind: "file", name: job.name, size: job.size, sent: job.sent || 0, jobId: job.id, dest: job.dest, chat, attach: !!chat, note: "" };
    if (job.state === "failed") { upsertUpload({ ...base, status: "error", error: job.error || stagingWords(job).failed }); continue; }
    const ctrl = new AbortController();
    upsertUpload({ ...base, status: "staging", error: "", ctrl });
    settleUpload(id, { status: "staging", job_id: job.id, name: job.name, size: job.size }, job.name, job.size, ctrl, !!chat)
      .catch((err) => failUpload(id, err, job.name, ctrl)).finally(() => setUploadProgress(id, null));
  }
  for (const f of d.folders || []) {
    // not registered = the browser had not sent every file: the "interrupted" card below covers it
    if (f.recorded || !f.registered || tracked.has(`folder:${f.name}`)) continue;
    const id = `jf${Math.abs(hashStr(f.name)).toString(36)}`;
    const chat = f.conversation_id || null;
    const base = { id, kind: "folder", name: `${shownFolder(f.name, f.original_name)}/`, size: f.size || 0, sent: f.sent || 0, filesDone: f.done || 0,
      filesTotal: f.count || 0, serverFolder: f.name, dest: f.dest, chat, attach: !!chat, note: "" };
    if (!f.pending && f.failed) { upsertUpload({ ...base, status: "error", error: `${f.failed} of ${f.count} files ${stagingWords(f).missing}` }); continue; }
    const ctrl = new AbortController();
    upsertUpload({ ...base, status: "staging", error: "", ctrl });
    followFolder(id, f.name, {}, ctrl, !!chat).catch((err) => failUpload(id, err, `${f.name}/`, ctrl)).finally(() => setUploadProgress(id, null));
  }
}
async function restorePendingCards() {
  const owner = apiKeyOwner();
  const stored = new Set();                            // the server's ids of every upload this browser keeps
  if (window.tus && window.tus.defaultOptions && window.tus.defaultOptions.urlStorage) {
    const storage = window.tus.defaultOptions.urlStorage;
    for (const up of await storage.findAllUploads()) {
      if (up.uploadUrl) stored.add(tusId(up.uploadUrl));
      // key: tus::<fingerprint>::<n>, fingerprint: tus:<owner>:<name or path>:<size>:<modified>
      const fingerprint = String(up.urlStorageKey || "").split("::")[1] || "";
      const meta = up.metadata || {};
      if (!fingerprint.startsWith(`tus:${owner}:`) || meta.rel_path || !up.uploadUrl) continue;   // a folder's file: its folder card speaks for it
      const probe = await probeTusUpload(up.uploadUrl);
      if (tusGone(probe.status)) { await storage.removeUpload(up.urlStorageKey); continue; }       // gone on the server
      const offset = probe.offset, length = probe.length > 0 ? probe.length : (up.size || 0);
      if (offset < 0) continue;                                         // no answer now (5xx, 401, offline): ask again next time
      if (offset >= length) { if (probe.status === 200) await storage.removeUpload(up.urlStorageKey); continue; }   // complete
      const name = meta.filename || "upload";
      if (state.uploads.some((u) => u.name === name && u.size === length && u.status !== "pending")) continue;   // already running again
      const id = `p${Math.abs(hashStr(fingerprint)).toString(36)}`;
      if (state.uploads.some((u) => u.id === id && (u.sent || 0) >= offset)) continue;   // one file stored twice: show the one further along
      upsertUpload({ id, kind: "file", name, size: length, sent: offset, chat: meta.conversation_id || null,
        status: "pending", error: "", note: `Interrupted at ${Math.floor((100 * offset) / length)}% — choose the file again to continue`,
        tusUrl: up.uploadUrl, tusKey: up.urlStorageKey });
    }
  }
  let keys = [];
  try { keys = Object.keys(localStorage).filter((k) => k.startsWith(`upload-folder:${owner}:`) && !k.endsWith(":completed")); } catch { /* private mode */ }
  for (const key of keys) {
    const id = "F" + Math.abs(hashStr(key)).toString(36);
    if (state.uploads.some((u) => u.id === id)) continue;
    const parts = key.split(":");                       // upload-folder : owner : name… : count : signature
    const count = Number(parts[parts.length - 2]) || 0;
    let done = 0;
    try { for (const c of atob(localStorage.getItem(`${key}:completed`) || "")) { let b = c.charCodeAt(0); while (b) { done += b & 1; b >>= 1; } } } catch { /* no bitmap yet */ }
    upsertUpload({ id, kind: "folder", name: `${localStorage.getItem(key) || parts.slice(2, -2).join(":")}/`, size: 0, sent: 0,
      filesDone: done, filesTotal: count, status: "pending", error: "", folderKey: key,
      note: `Interrupted — ${done} of ${count} files uploaded; choose the folder again to continue` });
  }
  await restoreServerCards(stored);
}
// The account's uploads left part-way where this browser stored nothing: another computer, a private
// window, or this browser before its site data was cleared. The server lists them (GET
// /api/tus/unfinished); without a card nothing said they wait. Choosing the file or folder again
// continues each one (findServerUpload) and Discard deletes it. An upload this browser stores, or one
// a card here already stands for, is left to that card; so is a file or folder that has a card here.
async function restoreServerCards(stored, request = fetch) {
  let d = null;
  try {
    const r = await request(`/api/tus/unfinished?connection_id=${encodeURIComponent(state.connectionId)}`);
    d = r.ok ? await r.json() : null;
  } catch { /* no answer: the cards this browser keeps are all there is */ }
  if (!d) return;
  const shown = new Set(state.uploads.flatMap((u) => [u.tusUrl, ...(u.tusUrls || [])]).filter(Boolean).map(tusId));
  const fresh = (up) => !!(up && up.url && up.size > 0) && !stored.has(tusId(up.url)) && !shown.has(tusId(up.url));
  for (const up of (d.files || []).filter(fresh)) {
    if (state.uploads.some((u) => u.kind === "file" && u.name === up.name && u.size === up.size)) continue;
    upsertUpload({ id: `s${tusId(up.url)}`, kind: "file", name: up.name, size: up.size, sent: up.offset,
      status: "pending", error: "", tusUrl: up.url, chat: up.conversation_id || null,
      note: `Interrupted at ${Math.floor((100 * up.offset) / up.size)}% in another browser — choose the file again to continue` });
  }
  for (const f of d.folders || []) {
    const parts = (f.files || []).filter(fresh);
    if (!f.folder || !parts.length || state.uploads.some((u) => u.kind === "folder" && u.name === `${f.folder}/`)) continue;
    const n = parts.length;
    upsertUpload({ id: `sf${Math.abs(hashStr(f.folder)).toString(36)}`, kind: "folder", name: `${f.folder}/`,
      size: parts.reduce((s, p) => s + p.size, 0), sent: parts.reduce((s, p) => s + p.offset, 0),
      status: "pending", error: "", partsFolder: f.folder, tusUrls: parts.map((p) => p.url),
      chat: (parts.find((p) => p.conversation_id) || {}).conversation_id || null,
      note: `Interrupted in another browser — ${n} ${n === 1 ? "file" : "files"} part-way; choose the folder again to continue` });
  }
}

// ---- dataset selection: one active chip + a "Data" popover -------------------
// The composer shows only the CURRENTLY SELECTED dataset as a removable chip; uploading a
// file/folder or picking a past upload happens in the "＋ Data" popover, so the recent-
// files history no longer clutters the composer. All uploads stay reachable to the agent.
// The bind-set is a SET, not a single slot (feature ②): attaching a file ADDS it (an upload, or a
// pick from the recent list), and a run can carry several. `_PRIMARY_SUFFIXES` mirrors the server's
// ranking so the chip row can mark which file is the primary (the one the legacy tools read).
const _PRIMARY_SUFFIXES = [".h5ad", ".vcf.gz", ".vcf", ".bcf", ".h5", ".loom", ".csv", ".tsv", ".txt"];
function _suffixRank(name) {
  const low = (name || "").toLowerCase();
  let best = _PRIMARY_SUFFIXES.length;
  _PRIMARY_SUFFIXES.forEach((s, i) => { if (low.endsWith(s) && i < best) best = i; });
  return best;
}
// The primary = highest-ranked bound file (attach order breaks ties) — mirrors the server's
// _bound_primary_key: a file marked "reference" is ranked after every other file.
function primaryBoundPath() {
  if (!state.boundPaths.length) return null;
  const chosen = state.boundPaths.find((p) => state.boundRoles[p] === "primary");
  if (chosen) return chosen;
  const meta = state.boundPaths.map((p, i) => {
    const d = state.datasets.find((x) => x.path === p);
    return { p, i, ref: state.boundRoles[p] === "reference" ? 1 : 0, r: _suffixRank(d ? d.name : p) };
  });
  meta.sort((a, b) => a.ref - b.ref || a.r - b.r || a.i - b.i);
  return meta[0].p;
}

// A dataset row's kind → its icon. Server kinds are bound by path from the gateway host's disks.
function isFolderKind(kind) { return kind === "folder" || kind === "server-folder"; }
function datasetIcon(kind) {
  return { folder: "folder", "server-folder": "folder_shared", "server-file": "dns" }[kind] || "draft";
}

// ---- data already on the server, bound by path (no upload) ---------------------------------
// The gateway allowlists folders (AISCIENTIST_SERVER_DATA_ROOTS). A path inside one, pasted into the
// chat, is offered above the composer (the Data menu no longer has an item for it: users do not see
// server paths, issue 21). Binding
// is always the user's click: a path the model reads in a message is never bound on its own. A run
// copies what it needs to HPC3 when it starts (only what changed since the last copy).
const _SERVER_PATH_RE = /(?<![\w/])(\/[^\s'"`<>|;,()[\]{}]+)/g;

async function loadServerRoots() {
  try {
    const d = await (await fetch("/api/server-data/roots")).json();
    state.serverRoots = d.enabled ? (d.roots || []) : [];
  } catch { state.serverRoots = []; }
  renderServerPathOffer();
}

function serverPathsIn(text) {
  const out = [];
  if (!state.serverRoots.length) return out;
  // Copied from wrapped text, a path arrives split at a slash ("…/jinl14/ CellQC_testdata/Sample1_WT"):
  // rejoin it when what follows the break is itself a relative path, never plain words.
  text = (text || "").replace(/\/\s+(?=[^\s/'"`<>|;,()[\]{}]+\/)/g, "/");
  for (const m of text.matchAll(_SERVER_PATH_RE)) {
    const p = m[1].replace(/[.:]+$/, "").replace(/\/+$/, "");
    if (state.serverRoots.some((r) => p === r || p.startsWith(r + "/")) && !out.includes(p)) out.push(p);
  }
  return out;
}

function renderServerPathOffer() {
  const host = $("serverPathOffer"), input = $("chatInput");
  if (!host || !input) return;
  const offers = serverPathsIn(input.value).filter((p) =>
    !state.boundPaths.includes(p) && !state.serverBoundTyped.has(p) && !state.serverOfferDismissed.has(p));
  if (!offers.length) { host.classList.add("hidden"); host.innerHTML = ""; return; }
  const p = offers[0];
  // A long path shows its last segments (the dataset's own name); the whole path is the tooltip.
  let shown = p;
  if (shown.length > 60) { const tail = shown.slice(-56); shown = "…" + tail.slice(Math.max(tail.indexOf("/"), 0)); }
  host.innerHTML = `<span class="material-symbols-outlined msym">folder_shared</span>` +
    `<span class="spo-text" title="${escapeHtml(p)}">Bind <code>${escapeHtml(shown)}</code> as data for this run?</span>` +
    (offers.length > 1 ? `<span class="spo-more">+${offers.length - 1} more</span>` : "") +
    `<button type="button" class="primary small" data-spo-bind="${escapeHtml(p)}">Bind</button>` +
    `<button type="button" class="ghost small" data-spo-dismiss="${escapeHtml(p)}">Not now</button>`;
  host.classList.remove("hidden");
}

async function bindServerPath(path) {
  path = (path || "").trim();
  if (!path) return false;
  if (!state.connectionId) { toast("Connect first: binding server data needs a session."); return false; }
  const sid = state.activeId, line = `bind:${sid}`;   // the chat it was asked in, which may be left meanwhile
  // The Bind bar still offers Bind while that chat's card waits: a second click would scan the
  // folder again and then be refused, so it says what it waits for instead.
  const waitingText = "A Bind is already waiting for your answer in this chat.";
  if (bindWaiting(chatOf(sid))) { toast(waitingText); return false; }
  const post = async (url) => {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, path, conversation_id: String(sid || "") }) });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.error || `HTTP ${res.status}`);
    return d;
  };
  try {
    setUploadProgress(line, `Checking ${path} on the server…`, sid);
    const info = await post("/api/server-data/check");
    setUploadProgress(line, "");
    const c = chatOf(sid);
    const clean = (p) => String(p || "").replace(/\/+$/, "");
    const binding = clean(info.path || path), overlaps = [];
    for (const bound of new Set((c && c.bound || []).map(clean))) {
      if (!bound || bound === binding) continue;
      const direction = binding.startsWith(bound + "/") ? "Inside"
        : bound.startsWith(binding + "/") ? "Contains" : "";
      if (direction) overlaps.push(`${direction} ${bound.split("/").pop()}/, already bound in this chat: its libraries are QC'd once.`);
    }
    const message = (base) => [base, ...overlaps].filter(Boolean).join("\n");
    // Large copies are confirmed first: the size and what will be skipped are in the message.
    if (info.confirm) {
      // Two clicks whose checks were both in flight: the first one's card is the one to answer.
      if (bindWaiting(c)) { toast(waitingText); return false; }
      if (!await confirmServerBind(c, message(info.message))) {
        toast("Bind cancelled.");
        return false;
      }
    }
    const d = await post("/api/server-data/bind");
    if (c) c.serverBoundTyped.add(path);
    addDataset({ name: d.name + (isFolderKind(d.kind) ? "/" : ""), path: d.path, kind: d.kind,
      gist: message(d.info && d.info.message) }, sid);
    toast(message(d.info && d.info.message ? d.info.message : `Bound ${d.name}`));
    renderServerPathOffer();
    return true;
  } catch (err) {
    setUploadProgress(line, "");
    toast("Can't bind that path: " + err.message);
    return false;
  }
}

// Mark/unmark one bound file and save the chat attachment set.
// Server-backed chats keep their attachment set on the account, not in browser storage.
function hydrateChatDatasets(c, data) {
  if (Number.isInteger(data.max_bound_datasets)) state.maxBoundDatasets = data.max_bound_datasets;
  if (!Array.isArray(data.datasets)) return;
  c.bound = data.datasets.map((d) => d.path);
  c.roles = Object.fromEntries(data.datasets.filter((d) => d.role).map((d) => [d.path, d.role]));
  for (const d of data.datasets) {
    const known = state.datasets.find((x) => x.id === d.id || x.path === d.path);
    if (known) Object.assign(known, d); else state.datasets.push(d);
  }
  saveComposer(c);
}

function datasetLimitAllows(paths) {
  if (state.maxBoundDatasets && paths.length > state.maxBoundDatasets) {
    toast(`Up to ${state.maxBoundDatasets} datasets per run. Remove one first.`);
    return false;
  }
  return true;
}

function changeChatDatasets(c, change) {
  if (!c) return Promise.resolve(false);
  const apply = async () => {
    const next = { bound: [...c.bound], roles: { ...c.roles } };
    change(next);
    if (!datasetLimitAllows(next.bound)) return false;
    const s = state.sessions.find((x) => String(x.id) === String(c.sid));
    try {
      if (s && s.cid) {
        if (next.bound.some((path) => !state.datasets.some((d) => d.path === path && d.id != null))) {
          const listing = await api("GET", "/api/datasets");
          if (Number.isInteger(listing.max_bound_datasets)) state.maxBoundDatasets = listing.max_bound_datasets;
          for (const d of listing.datasets || []) {
            const known = state.datasets.find((x) => x.path === d.path);
            if (known) Object.assign(known, d); else state.datasets.push(d);
          }
          if (!datasetLimitAllows(next.bound)) return false;
        }
        const datasets = next.bound.map((path) => {
          const d = state.datasets.find((x) => x.path === path);
          if (!d || d.id == null) throw new Error("This dataset is not saved yet. Refresh Files and try again.");
          return { id: d.id, role: next.roles[path] || null };
        });
        const data = await api("PUT", `/api/conversations/${s.cid}/datasets`, { datasets });
        hydrateChatDatasets(c, data);
      } else { c.bound = next.bound; c.roles = next.roles; saveComposer(c); }
      if (chatVisible(c)) syncDatasetSelection();
      return true;
    } catch (err) { toast(`Datasets not saved: ${err.message}`); return false; }
  };
  c.datasetSave = (c.datasetSave || Promise.resolve()).then(apply);
  return c.datasetSave;
}

function toggleReference(path) {
  return changeChatDatasets(activeChat(), (c) => {
    if (c.roles[path] === "reference") delete c.roles[path]; else c.roles[path] = "reference";
  });
}

function makeMainDataset(path) {
  return changeChatDatasets(activeChat(), (c) => {
    if (!c.bound.includes(path)) return;
    for (const p of Object.keys(c.roles)) if (c.roles[p] === "primary") delete c.roles[p];
    c.roles[path] = "primary";
  });
}

// Uploads attach to the chat they started in, even when it is no longer on screen.
function addDataset(d, sid) {
  if (!d || !d.path) return Promise.resolve(false);
  const known = state.datasets.find((x) => x.path === d.path);
  if (known) Object.assign(known, d); else state.datasets.unshift(d);
  return changeChatDatasets(chatOf(sid || state.activeId), (c) => {
    if (!c.bound.includes(d.path)) c.bound.push(d.path);
  });
}

async function useSelectedDatasets() {
  const c = activeChat();
  const selected = state.datasets.filter((d) => state.selectedDatasetIds.has(String(d.id)));
  if (!selected.length) return;
  const saved = await changeChatDatasets(c, (next) => {
    next.bound = [...new Set([...next.bound, ...selected.map((d) => d.path)])];
  });
  if (saved) {
    state.selectedDatasetIds.clear();
    openChat(c.sid);
    renderDatasetSelectionCount();
  }
}

function renderDatasetSelectionCount() {
  const button = $("datasetsUseSelected"), limit = $("datasetsLimit");
  if (button) {
    button.disabled = !state.selectedDatasetIds.size;
    button.textContent = `Use selected (${state.selectedDatasetIds.size})`;
  }
  if (limit) limit.textContent = state.maxBoundDatasets ? `Up to ${state.maxBoundDatasets} datasets per run` : "";
}

// A deleted dataset leaves every chat it was attached to, the ones not opened since the reload too.
function unbindEverywhere(path) {
  state.datasets = state.datasets.filter((d) => d.path !== path);
  for (const s of state.sessions) {
    const c = chatOf(s.id);
    if (!c.bound.includes(path)) continue;
    c.bound = c.bound.filter((p) => p !== path);
    delete c.roles[path];
    saveComposer(c);
  }
  syncDatasetSelection();
}

// A dataset whose path changed (moved from HPC3 to the server) stays attached wherever it was.
function repointEverywhere(from, to) {
  state.datasets = state.datasets.filter((d) => d.path !== from || d.path === to);
  for (const s of state.sessions) {
    const c = chatOf(s.id);
    if (!c.bound.includes(from)) continue;
    c.bound = [...new Set(c.bound.map((p) => (p === from ? to : p)))];
    if (from in c.roles) { c.roles[to] = c.roles[from]; delete c.roles[from]; }
    saveComposer(c);
  }
}

// The chat an upload attaches to: the one it was started from. The server keeps it too, so a card
// restored after a reload knows it; one from before the server kept it has none and attaches nothing.
function uploadChat(id) {
  const u = state.uploads.find((x) => x.id === id);
  return (u && u.chat) || null;
}
// What an upload request tells the server about its chat (tus metadata and the folder calls).
function uploadChatField(id) {
  const sid = uploadChat(id);
  return sid ? { conversation_id: String(sid) } : {};
}
// "from “Retina scRNA”" for the Datasets page: { text, open } (open = the chat still exists), or null.
function chatLabel(sid) {
  if (!sid) return null;
  const s = state.sessions.find((x) => String(x.id) === String(sid));
  return s ? { text: `from “${s.title || "New chat"}”`, open: true } : { text: "from a deleted chat", open: false };
}
function openChat(sid) {
  if (!sid || !state.sessions.some((s) => String(s.id) === String(sid))) return;
  switchView("research");
  if (String(state.activeId) !== String(sid)) selectSession(String(sid));
}

// Add/remove one path from the bind-set (a recent-list toggle or a chip ×).
function toggleBound(path) {
  if (!path) return;
  return changeChatDatasets(activeChat(), (c) => {
    if (c.bound.includes(path)) { c.bound = c.bound.filter((p) => p !== path); delete c.roles[path]; }
    else c.bound.push(path);
  });
}

function clearBound() { return changeChatDatasets(activeChat(), (c) => { c.bound = []; c.roles = {}; }); }

function syncDatasetSelection() {
  state.datasetPath = primaryBoundPath();
  $("datasetInput").value = state.datasetPath || "";   // legacy primary, for back-compat readers
  // Kept per chat across a reload (saveComposer); a new chat starts with an empty bind-set rather
  // than silently re-attaching the last one (see loadDatasetChips).
  saveComposer(activeChat());
  renderDatasetChips();
}
// Back-compat alias: some call sites still say selectDataset(path|null) — a single-file select is just
// "replace the set with this one" (or clear it).
function selectDataset(path) { return changeChatDatasets(activeChat(), (c) => { c.bound = path ? [path] : []; c.roles = {}; }); }

function renderDatasetChips() {
  const host = $("datasetChips");
  if (!host) return;
  if (!state.boundPaths.length) { host.innerHTML = '<span class="dataset-empty">No dataset selected</span>'; return; }
  const primary = primaryBoundPath();
  host.innerHTML = state.boundPaths.map((path) => {
    const d = state.datasets.find((x) => x.path === path) || { name: path.split("/").pop(), path, kind: "file" };
    const ico = datasetIcon(d.kind);
    const isPrimary = path === primary;
    const isRef = state.boundRoles[path] === "reference";
    const mainBtn = !isPrimary ? `<button type="button" class="dataset-chip-ref" data-main-path="${escapeHtml(path)}" title="Use as the main dataset">Make main</button>` : '<span class="dataset-chip-role">main</span>';
    const gist = d.gist ? ` — ${d.gist}` : "";                 // the file's ① skim, when known
    // The reference toggle appears once a second file is attached: one file is always the study.
    const refBtn = state.boundPaths.length > 1
      ? `<button type="button" class="dataset-chip-ref${isRef ? " on" : ""}" data-ref-path="${escapeHtml(path)}" ` +
        `title="${isRef ? "Reference data — click to use it as an ordinary file" : "Mark as reference data (e.g. a cell-type atlas to compare against)"}">ref</button>`
      : "";
    return `<span class="dataset-chip active${isRef ? " reference" : ""}" title="${escapeHtml(d.path + gist)}">` +
      `<span class="material-symbols-outlined msym">${ico}</span>` +
      `<span class="dataset-chip-name">${escapeHtml(d.name)}${isPrimary ? " ★" : ""}</span>` + mainBtn + refBtn +
      `<button type="button" class="dataset-chip-x" data-clear-path="${escapeHtml(path)}" title="Remove this file">×</button></span>`;
  }).join("");
}

// The composer's upload line belongs to the chat the upload was started from (issue 7). Each upload
// has its own line; the composer shows only those of the chat on screen. Another chat's upload goes
// on meanwhile, its progress is there again when that chat is opened, and the Datasets page lists
// every upload with the chat it came from.
const uploadLines = new Map();          // upload id (or "bind:<chat>") -> { chat, text }
function setUploadProgress(id, text, chat) {
  if (!text) uploadLines.delete(id);
  else {
    const had = uploadLines.get(id);
    const sid = chat || uploadChat(id) || (had && had.chat) || "";
    uploadLines.set(id, { chat: String(sid), text });
  }
  renderUploadLine();
}
// Uploads in flight that were started from chat `sid`.
function chatUploads(sid) {
  return state.uploads.filter((u) => u.chat && String(u.chat) === String(sid) && UPLOAD_ACTIVE.includes(u.status));
}
function renderUploadLine() {
  const el = $("uploadStatus"), cb = $("uploadCancel");
  if (!el) return;
  const here = String(state.activeId || "");
  const lines = here ? [...uploadLines.values()].filter((l) => l.chat === here) : [];
  const text = lines.length ? lines[0].text + (lines.length > 1 ? ` (+${lines.length - 1} more)` : "") : "";
  el.classList.toggle("hidden", !text);
  el.textContent = text;
  // Cancel stops this chat's uploads, so it is there only while one of them is in flight.
  if (cb) cb.classList.toggle("hidden", !text || !chatUploads(here).length);
}

// "＋ Data" opens a two-item list, Files and Folder (issue 21): a browser's file dialog picks either
// files or one folder, never both. Several files and folders at once are dropped onto the chat.
function toggleDataMenu(force) {
  const m = $("dataMenu");
  if (!m) return;
  const show = force !== undefined ? force : m.classList.contains("hidden");
  m.classList.toggle("hidden", !show);
}

// ---- dropping files and folders onto the chat ------------------------------------------------------
// The drop's entries are read while the event is being handled (the browser empties the DataTransfer
// after it); the folders are then walked. A file goes up as an upload of its own, a folder as one
// folder upload, as if chosen in the dialogs. A dropped file has no webkitRelativePath, so each file
// of a folder gets one: its path under the dropped folder, as the folder dialog gives it.
function dropEntries(dt) {
  const items = [...((dt && dt.items) || [])].filter((it) => it.kind === "file");
  const entries = items.map((it) => (typeof it.webkitGetAsEntry === "function" ? it.webkitGetAsEntry() : null));
  if (entries.length && entries.every(Boolean)) return { entries, files: [] };
  return { entries: [], files: [...((dt && dt.files) || [])] };   // no entry API: plain files only
}
function entryFile(entry) { return new Promise((res, rej) => entry.file(res, rej)); }
async function readDirAll(dir) {
  const reader = dir.createReader(), out = [];
  for (;;) {      // readEntries hands the entries over in batches until it returns an empty one
    const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
    if (!batch.length) return out;
    out.push(...batch);
  }
}
async function folderFiles(dir) {
  const files = [], todo = [dir];
  while (todo.length) {
    const d = todo.shift();
    for (const e of await readDirAll(d)) {
      if (e.isDirectory) { todo.push(e); continue; }
      if (!e.isFile || e.name === ".DS_Store") continue;
      const f = await entryFile(e);
      const rel = String(e.fullPath || `${dir.name}/${e.name}`).replace(/^\/+/, "");
      try { Object.defineProperty(f, "webkitRelativePath", { value: rel, configurable: true }); } catch { /* read-only here */ }
      files.push(f);
    }
  }
  return files;
}
// What a drop holds: the loose files, and each folder's files. Reading a folder that fails is skipped
// with a note; the rest goes up.
async function droppedUploads(found) {
  const files = [...found.files], folders = [];
  for (const e of found.entries) {
    try {
      if (e.isDirectory) { const fl = await folderFiles(e); if (fl.length) folders.push(fl); }
      else if (e.isFile) files.push(await entryFile(e));
    } catch (err) { toast(`Couldn't read ${e.name}: ${err.message || err}`); }
  }
  return { files, folders };
}
async function onDataDrop(found) {
  if (!state.connectionId) { toast("Connect to HPC3 first."); return; }
  const { files, folders } = await droppedUploads(found);
  if (!files.length && !folders.length) { toast("Nothing to upload in what was dropped."); return; }
  for (const f of files) uploadDataset(f);
  for (const fl of folders) uploadFolder(fl);
}
function wireDataDrop() {
  const panel = document.querySelector(".chat-panel"), hint = $("dropHint");
  if (!panel) return;
  let depth = 0;
  const carriesFiles = (e) => [...((e.dataTransfer && e.dataTransfer.types) || [])].includes("Files");
  const show = (on) => { if (hint) hint.classList.toggle("hidden", !on); panel.classList.toggle("drop-target", on); };
  panel.addEventListener("dragenter", (e) => { if (!carriesFiles(e)) return; e.preventDefault(); depth += 1; show(true); });
  panel.addEventListener("dragover", (e) => { if (!carriesFiles(e)) return; e.preventDefault(); e.dataTransfer.dropEffect = "copy"; });
  panel.addEventListener("dragleave", (e) => { if (!carriesFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) show(false); });
  panel.addEventListener("drop", (e) => {
    if (!carriesFiles(e)) return;
    e.preventDefault();
    depth = 0;
    show(false);
    onDataDrop(dropEntries(e.dataTransfer));
  });
}

// Load this user's server-side dataset history so earlier uploads (incl. folders from a previous
// visit) are known to the page: their chips, and Datasets → Use, which attaches one to the open chat.
async function loadDatasetChips() {
  try {
    const data = await (await fetch("/api/datasets")).json();
    if (Number.isInteger(data.max_bound_datasets)) state.maxBoundDatasets = data.max_bound_datasets;
    const items = data.datasets || [];
    for (const x of items) {
      const name = isFolderKind(x.kind) ? x.name + "/" : x.name;
      // A dataset moved from HPC3 to the server keeps its id and changes its path: the chip and every
      // chat it is attached to follow it, rather than keep sending the HPC3 original.
      const moved = x.id != null && state.datasets.find((d) => d.id === x.id && d.path !== x.path);
      if (moved) repointEverywhere(moved.path, x.path);
      const known = state.datasets.find((d) => d.path === x.path);
      if (known) { if (known.id == null) known.id = x.id; continue; }
      state.datasets.push({ id: x.id, name, path: x.path, kind: x.kind, at: x.uploaded_at, size: x.size_bytes });
    }
  } catch { /* datasets need accounts; the list just stays empty otherwise */ }
  // Do NOT auto-select the last dataset: a fresh chat starts with an EMPTY dataset box rather than
  // silently inheriting whatever was loaded last time (that surprised users and mis-attached data to
  // a new question). Prior uploads still appear as clickable chips; the user picks one explicitly.
  renderDatasetChips();
}

function hashStr(s) {
  let h = 0;
  for (let i = 0; i < s.length; i++) { h = (h * 31 + s.charCodeAt(i)) | 0; }
  return h;
}

function syncAuthMethod() {
  const key = $("authMethod").value === "ssh_key";
  $("passwordRow").classList.toggle("hidden", key);
  $("duoMethodRow").classList.toggle("hidden", key);
  $("createKeyRow").classList.toggle("hidden", key);
  $("credRow").classList.toggle("hidden", !key);
  $("keyPassRow").classList.toggle("hidden", !key);
  syncNewKeyPass();
  if (key) loadCredentials();
}

// The "protect the new key" passphrase only applies when we're actually minting a key
// (password+Duo login with "Remember me" checked).
function syncNewKeyPass() {
  const minting = $("authMethod").value !== "ssh_key" && $("createKeyCheck") && $("createKeyCheck").checked;
  $("newKeyPassRow").classList.toggle("hidden", !minting);
}

// Fill the "Saved SSH key" dropdown with the caller's stored credentials (public metadata).
async function loadCredentials() {
  const sel = $("credSelect");
  if (!sel) return;
  const user = $("userInput").value.trim();
  try {
    const q = user ? `?user=${encodeURIComponent(user)}` : "";
    const creds = (await (await fetch("/api/ssh-credentials" + q)).json()).credentials || [];
    if (!creds.length) {
      sel.innerHTML = '<option value="">No saved keys yet — log in with password + Duo once</option>';
      return;
    }
    sel.innerHTML = creds.map((c) =>
      `<option value="${c.id}">${escapeHtml(c.label)}${c.encrypted ? " 🔒" : ""}</option>`).join("");
  } catch { sel.innerHTML = '<option value="">Could not load saved keys</option>'; }
}

// ---- collapsible panels ---------------------------------------------------

function applyUiPrefs() {
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(UI_KEY) || "{}"); } catch {}
  setPanel("left", prefs.left !== false);
  setPanel("right", prefs.right === true);   // downloads/log panel collapsed by default
}

function setPanel(side, open, remember = true) {
  $("layout").classList.toggle(`collapse-${side}`, !open);
  $(side === "left" ? "toggleLeft" : "toggleRight").classList.toggle("active", open);
  if (!remember) return;
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(UI_KEY) || "{}"); } catch {}
  prefs[side] = open;
  try { localStorage.setItem(UI_KEY, JSON.stringify(prefs)); } catch {}
}

function togglePanel(side) {
  const open = $("layout").classList.contains(`collapse-${side}`); // currently collapsed → open it
  setPanel(side, open);
}

// ---- auth + operon-style shell (login gate, nav rail, 5 views) ------------

let _appInited = false;
function fmtDate(iso) { if (!iso) return "—"; try { return new Date(iso).toLocaleString(); } catch { return iso; } }

async function bootstrap() {
  $("loginForm").addEventListener("submit", doLogin);
  wireRegister();
  wireChatScroll();
  let me = null;
  try { const r = await fetch("/api/auth/me"); if (r.ok) me = await r.json(); } catch {}
  if (me === null) { showApp(null); return; }          // accounts disabled on server → no gate
  if (me.authenticated) showApp(me.user); else showLogin();
}

// Track whether the reader is stuck to the bottom of the chat stream. Streaming auto-follows ONLY
// when stuck, so scrolling up to read history mid-run no longer yanks the view back down.
function wireChatScroll() {
  const cs = $("chatStream");
  if (!cs) return;
  state.stickBottom = true;
  cs.addEventListener("scroll", () => { state.stickBottom = nearBottom(cs); }, { passive: true });
}

// ---- self-registration (UCI email + emailed code) --------------------------
let _regEmail = "";           // the email the current pending code was sent to

function wireRegister() {
  const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
  on("registerForm", "submit", doRegisterStart);
  on("verifyForm", "submit", doRegisterVerify);
  on("showRegister", "click", (e) => { e.preventDefault(); showAuthPane("register"); });
  on("showLoginFromRegister", "click", (e) => { e.preventDefault(); showAuthPane("login"); });
  on("showLoginFromVerify", "click", (e) => { e.preventDefault(); showAuthPane("login"); });
  on("resendCode", "click", (e) => { e.preventDefault(); showAuthPane("register"); });
  // Only offer the "Create account" path when the server allows self-registration.
  fetch("/api/auth/config").then((r) => r.ok ? r.json() : null).then((cfg) => {
    if (cfg && cfg.self_register === false) { const f = $("loginFoot"); if (f) f.classList.add("hidden"); }
    if (cfg && cfg.email_domains && $("regEmail")) $("regEmail").placeholder = "you@" + cfg.email_domains[0];
  }).catch(() => {});
}

// Toggle between the three login-card panes (sign-in / register / verify).
function showAuthPane(pane) {
  $("loginForm").classList.toggle("hidden", pane !== "login");
  $("loginFoot").classList.toggle("hidden", pane !== "login");
  $("registerForm").classList.toggle("hidden", pane !== "register");
  $("verifyForm").classList.toggle("hidden", pane !== "verify");
  const focusId = { login: "loginUser", register: "regUser", verify: "verifyCode" }[pane];
  const el = $(focusId); if (el) el.focus();
}

async function doRegisterStart(e) {
  e.preventDefault();
  const err = $("registerError"); err.classList.add("hidden");
  const body = { username: $("regUser").value.trim(), email: $("regEmail").value.trim(), password: $("regPass").value };
  const btn = $("regBtn"); btn.disabled = true; const old = btn.textContent; btn.textContent = "Sending…";
  try {
    const r = await fetch("/api/auth/register/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));   // a bare 500 returns plain text, not JSON → {}
    if (!r.ok) { err.textContent = d.detail || `Could not start registration (server error ${r.status}). Please try again shortly.`; err.classList.remove("hidden"); return; }
    _regEmail = d.email;
    $("verifySub").textContent = `Enter the 6-digit code we sent to ${d.email} (expires in ${d.expires_in_minutes} min).`;
    const hint = $("devCodeHint");
    if (d.dev_mode && d.dev_code) { hint.textContent = `Dev mode (no SMTP configured): your code is ${d.dev_code}`; hint.classList.remove("hidden"); }
    else hint.classList.add("hidden");
    $("verifyCode").value = "";
    showAuthPane("verify");
  } catch { err.textContent = "Can't reach the server."; err.classList.remove("hidden"); }
  finally { btn.disabled = false; btn.textContent = old; }
}

async function doRegisterVerify(e) {
  e.preventDefault();
  const err = $("verifyError"); err.classList.add("hidden");
  const body = { email: _regEmail, code: $("verifyCode").value.trim() };
  const btn = $("verifyBtn"); btn.disabled = true; const old = btn.textContent; btn.textContent = "Verifying…";
  try {
    const r = await fetch("/api/auth/register/verify", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { err.textContent = d.detail || `Verification failed (server error ${r.status}). Please try again shortly.`; err.classList.remove("hidden"); return; }
    showApp(d.user);   // verified → account created + signed in
  } catch { err.textContent = "Can't reach the server."; err.classList.remove("hidden"); }
  finally { btn.disabled = false; btn.textContent = old; }
}

function showLogin() {
  $("loginOverlay").classList.remove("hidden");
  $("appShell").classList.add("hidden");
  showAuthPane("login");
}

function showApp(user) {
  state.user = user;
  $("loginOverlay").classList.add("hidden");
  $("appShell").classList.remove("hidden");
  $("userChip").textContent = user ? (user.username + (user.role === "admin" ? " · admin" : "")) : "accounts off";
  document.querySelectorAll(".admin-only").forEach((el) => el.classList.toggle("hidden", !user || user.role !== "admin"));
  const lo = $("leftLogoutBtn"); if (lo) lo.classList.toggle("hidden", !user);   // sign-out lives in the left panel now
  // Mock mode runs the agent's code on the gateway server, so with accounts on only an admin gets it.
  const mock = $("mockCheck");
  if (mock && mock.closest("label")) mock.closest("label").classList.toggle("hidden", !!user && user.role !== "admin");
  if (!_appInited) { init(); initShell(); _appInited = true; }
  renderAccount(user);
  switchView("research");
  checkBudgetNotices();
}

async function doLogin(e) {
  e.preventDefault();
  const username = $("loginUser").value.trim(), password = $("loginPass").value;
  const err = $("loginError"); err.classList.add("hidden");
  try {
    const r = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }) });
    if (!r.ok) { const d = await r.json().catch(() => ({})); err.textContent = d.detail || "Invalid username or password"; err.classList.remove("hidden"); return; }
    showApp((await r.json()).user);
  } catch { err.textContent = "Can't reach the server."; err.classList.remove("hidden"); }
}

async function doLogout() {
  try { await fetch("/api/auth/logout", { method: "POST" }); } catch {}
  try { localStorage.removeItem(COMPOSER_KEY); } catch {}   // drafts and attachments stay with the account
  location.reload();
}

function initShell() {
  document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
  $("datasetsRefresh").addEventListener("click", loadFiles);
  $("datasetsUseSelected").addEventListener("click", useSelectedDatasets);
  $("runsRefresh").addEventListener("click", loadRuns);
  $("runsList").addEventListener("click", onRunsClick);
  $("outputsList").addEventListener("click", onOutputsClick);
  document.querySelectorAll(".files-tab").forEach((b) => b.addEventListener("click", () => switchFilesTab(b.dataset.filesTab)));
  wireListSearch("datasetsSearch", "datasetsList", "datasetsCount");
  wireListSearch("outputsSearch", "outputsList", "outputsCount");
  wireListSearch("runsSearch", "runsList", "runsCount");
  $("adminRefresh").addEventListener("click", loadAdminUsers);
  $("systemRefresh").addEventListener("click", loadSystem);
  $("systemRuns").addEventListener("click", onSystemRunsClick);
  wireSysCatalog();
  document.addEventListener("visibilitychange", () => { if (!document.hidden && sysLive.timer) pollSystemLive(); });
  $("createUserForm").addEventListener("submit", createUser);
  $("changePwForm").addEventListener("submit", changePassword);
  $("logoutBtn").addEventListener("click", doLogout);
  $("userTableBody").addEventListener("click", onUserTableClick);
  let _userSearchTimer = null;
  $("userSearch").addEventListener("input", () => { clearTimeout(_userSearchTimer); _userSearchTimer = setTimeout(loadAdminUsers, 250); });
  $("userSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); clearTimeout(_userSearchTimer); loadAdminUsers(); } });
  $("userSearchClear").addEventListener("click", () => { $("userSearch").value = ""; loadAdminUsers(); });
  document.querySelectorAll(".admin-tab").forEach((b) => b.addEventListener("click", () => switchAdminTab(b.dataset.adminTab)));
  $("skillReviewRefresh").addEventListener("click", loadSkillReviews);
  document.querySelectorAll(".skill-chip").forEach((b) => b.addEventListener("click", () => {
    skillReview.filter = b.dataset.skillFilter;
    document.querySelectorAll(".skill-chip").forEach((c) => c.classList.toggle("active", c === b));
    renderSkillReviews();
  }));
  $("skillReviewList").addEventListener("click", onSkillReviewClick);
  $("skillRequestRefresh").addEventListener("click", loadSkillRequests);
  document.querySelectorAll(".req-chip").forEach((b) => b.addEventListener("click", () => {
    skillRequests.filter = b.dataset.reqFilter;
    document.querySelectorAll(".req-chip").forEach((c) => c.classList.toggle("active", c === b));
    loadSkillRequests();
  }));
  $("skillRequestList").addEventListener("click", onSkillRequestClick);
  $("datasetsList").addEventListener("click", onDatasetsClick);
  $("metadataForm").addEventListener("submit", saveMetadata);
  $("metadataForm").addEventListener("change", (e) => { if (e.target.id === "mdf_human_data") mdSyncShowIf(); });
  $("metadataClose").addEventListener("click", () => $("metadataModal").classList.add("hidden"));
  $("metadataModal").addEventListener("click", (e) => { if (e.target.id === "metadataModal") $("metadataModal").classList.add("hidden"); });
  $("mcpTokenForm").addEventListener("submit", createMcpToken);
  $("mcpTokens").addEventListener("click", onMcpTokensClick);
  $("uploadsPanel").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]");
    const card = b && b.closest("[data-upload-id]");
    if (card) uploadAction(b.dataset.act, card.dataset.uploadId);
  });
  const uc = $("uploadCancel");
  if (uc) uc.addEventListener("click", cancelActiveUploads);   // composer-side Cancel
  wireUsage();
}

function switchView(name) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "datasets") loadFiles();
  else if (name === "runs") loadRuns();
  else if (name === "admin") { loadAdminUsers(); loadSkillReviews(); loadSkillRequests(); }
  else if (name === "account") { renderAccount(state.user); loadUsage(); if (state.accountTab === "storage") loadStorageTab(); }
  else if (name === "system") loadSystem();
  if (name !== "system") stopSystemLive();
}

// ---- System view (capabilities / agents / pipelines, tools and skills) ----
async function loadSystem() {
  const el = $("systemBody");
  const query = ($("sysSearch") || {}).value || "";       // a Refresh keeps the search
  el.innerHTML = '<div class="empty-hint">Loading…</div>';
  try {
    const d = await (await fetch("/api/system")).json();
    const cap = (k, v) => `<span class="cap ${v ? "on" : "off"}">${v ? "✓" : "✕"} ${escapeHtml(k)}</span>`;
    const caps = Object.entries(d.capabilities || {}).map(([k, v]) => cap(k, v)).join("");
    const agents = (d.agents || []).map((a) =>
      `<div class="card"><div class="card-main"><strong>${escapeHtml(a.name)}</strong>` +
      `<span class="muted">${escapeHtml(a.role)} · ${escapeHtml(a.where)}</span>` +
      `<span class="sys-desc">${escapeHtml(a.description)}</span></div></div>`).join("");
    const specialists = (d.specialists || []).map((s) =>
      `<div class="card"><div class="card-main"><strong>${escapeHtml(s.name)}</strong>` +
      `<span class="sys-desc">${escapeHtml(s.persona)}</span></div></div>`).join("");
    const rel = d.release || {};
    // An rsync deploy has no release tag but knows its commit: "untagged (abc1234)".
    // skills: the release skills/current points at; "app pins" when that is not the app's own pin.
    const relRow = (label, r) => {
      const ok = !!(r && (r.tag || r.sha)) && !(r && (r.installed === false || r.error));
      const name = (r && r.tag) || (r && r.sha ? "untagged" : "unknown");
      return `<span class="cap ${ok ? "on" : "off"}">${escapeHtml(label)}: ${escapeHtml(name)}` +
        `${r && r.sha ? " (" + escapeHtml(String(r.sha).slice(0, 7)) + ")" : ""}` +
        `${r && r.installed === false ? " (not installed)" : ""}` +
        `${r && r.pinned ? " (app pins " + escapeHtml(r.pinned) + ")" : ""}` +
        `${r && r.error ? " (refused: " + escapeHtml(r.error) + ")" : ""}</span>`;
    };
    const schema = rel.schema;
    const schemaRow = schema
      ? `<span class="cap ${schema.database === schema.release ? "on" : "off"}">database schema: ` +
        `${escapeHtml(schema.database || "unversioned")}` +
        `${schema.database === schema.release ? "" : " (release has " + escapeHtml(schema.release) + ")"}</span>`
      : "";
    el.innerHTML =
      `<h3>Running versions</h3><div class="cap-row">${relRow("app", rel.app)}${relRow("skills", rel.skills)}` +
      `${schemaRow}</div>` +
      `<h3>Capabilities (this server)</h3><div class="cap-row">${caps}</div>` +
      `<p class="sys-note">What the web server has itself. A tool that runs on HPC3 brings its software in ` +
      `its job's container, so it is not checked here: each tool below says where it runs.</p>` +
      `<h3>Agents</h3><div class="card-list">${agents}</div>` +
      (specialists ? `<h3>Scientist specialists</h3><div class="card-list">${specialists}</div>` : "") +
      sysCatalogHtml(d);
    if (query) { $("sysSearch").value = query; sysCatalogFilter(query); }
    renderWorkflowGraph(d.graph);
    startSystemLive();
  } catch { el.innerHTML = '<div class="empty-hint">Failed to load system overview.</div>'; }
}

// Pipelines, tools and skills: one line each (name, summary, where it runs), the whole text when a
// row is opened, and one search over all three. A row's data-q holds everything the search reads.
function sysSearchText(parts) {
  const raw = parts.filter((p) => p !== undefined && p !== null && p !== "").join(" \n ").toLowerCase();
  return raw + " \n " + raw.replace(/[_\-·]+/g, " ");      // "cell type" finds cell-type and cell_type
}

function sysMatch(query, text) {
  const terms = String(query || "").toLowerCase().split(/\s+/).filter(Boolean);
  return terms.every((t) => text.includes(t));
}

function sysKv(rows) {
  const html = rows.filter(([, v]) => v).map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${v}</dd>`).join("");
  return html ? `<dl class="sys-kv">${html}</dl>` : "";
}

const sysCode = (s) => `<code>${escapeHtml(s)}</code>`;

// Whether an opened row's full text starts with its summary line, which then need not show twice.
function sysRepeats(summary, full) {
  const head = String(summary || "").replace(/…$/, "").replace(/[*`]/g, "").trim();
  return !!head && String(full || "").replace(/[*`]/g, "").replace(/\s+/g, " ").startsWith(head);
}

function sysPipelineItem(p) {
  const type = METHOD_DATA_TYPES[p.data_type] || p.data_type || "";
  const tools = (p.tools || []).map((t) =>
    `<button type="button" class="sys-q" data-sys-tool="${escapeHtml(t)}" title="Show ${escapeHtml(t)} in Tools">${escapeHtml(t)}</button>`).join("");
  const long = p.description && p.description !== p.summary ? `<div class="sys-md">${renderMarkdown(p.description)}</div>` : "";
  return {
    id: `pipeline:${p.key}`, name: p.title || p.key, summary: p.summary, repeats: !!long && sysRepeats(p.summary, p.description),
    tags: type ? [{ text: type }] : [],
    body: long + sysKv([["Tools", tools && `<div class="sys-qs">${tools}</div>`], ["Key", sysCode(p.key)],
                        ["Protocol", sysCode(`preset_pipelines/${p.key}/`)]]),
    text: sysSearchText([p.title, p.key, p.summary, p.description, type, ...(p.tools || [])]),
  };
}

function sysToolItem(t) {
  const tags = [{ text: t.where || t.runs_on || "", title: "Where it runs" }];
  if (t.available === false) tags.push({ text: "unavailable here", cls: "off" });
  if (t.reads_private_data) tags.push({ text: "private data", cls: "priv", title: "Reads the user's data" });
  return {
    id: `tool:${t.name}`, name: t.name, mono: true, summary: t.summary || t.description, tags,
    body: `<div class="sys-k">What the model is told</div><div class="sys-md">${renderMarkdown(t.description)}</div>` +
      sysKv([["Runs on", escapeHtml(t.where || t.runs_on || "")], ["Category", escapeHtml(t.category || "")],
             ["Needs", (t.requires || []).map(escapeHtml).join(", ")],
             ["Here", typeof t.available === "boolean" ? (t.available ? "available on this server" : "not installed on this server") : ""],
             ["Code", t.code ? sysCode(t.code) : ""]]),
    text: sysSearchText([t.name, t.summary, t.description, t.category, t.where, t.runs_on, ...(t.requires || []),
                         t.reads_private_data ? "private data" : ""]),
  };
}

function sysSkillItem(s) {
  const tags = [];
  if (s.category) tags.push({ text: s.category });
  if (s.image) tags.push({ text: "own environment", title: s.image });
  if (s.induced) tags.push({ text: "induced", title: "Written by the system from a past run, approved by an admin" });
  const long = s.description && s.description !== s.summary ? `<div class="sys-md">${renderMarkdown(s.description)}</div>` : "";
  const use = s.use ? `<div class="sys-k">When to use</div><div class="sys-md">${renderMarkdown(methodUnwrap(s.use))}</div>` : "";
  return {
    id: `skill:${s.name}`, name: s.name, mono: true, summary: s.summary, tags, repeats: !!long && sysRepeats(s.summary, s.description),
    body: long + use + sysKv([["Files", (s.files || []).map(sysCode).join(" ")], ["Runs in", s.image ? sysCode(s.image) : ""],
                              ["Replaces", s.supersedes ? sysCode(s.supersedes) : ""]]),
    text: sysSearchText([s.name, s.summary, s.description, s.use, s.category, ...(s.files || [])]),
  };
}

function sysItemHtml(it) {
  const tags = (it.tags || []).filter((g) => g.text).map((g) =>
    `<span class="sys-tag${g.cls ? " " + g.cls : ""}"${g.title ? ` title="${escapeHtml(g.title)}"` : ""}>${escapeHtml(g.text)}</span>`).join("");
  return `<details class="sys-item${it.repeats ? " repeats" : ""}" data-id="${escapeHtml(it.id)}" data-q="${escapeHtml(it.text)}">` +
    `<summary><span class="sys-item-name${it.mono ? " mono" : ""}">${escapeHtml(it.name)}</span>` +
    `<span class="sys-item-sum">${escapeHtml(it.summary || "")}</span>` +
    (tags ? `<span class="sys-item-tags">${tags}</span>` : "") + `</summary>` +
    `<div class="sys-item-body">${it.body}</div></details>`;
}

function sysCatalogHtml(d) {
  const lists = [
    ["pipelines", "Pipelines", "The protocols the PI picks from for a request", (d.pipelines || []).map(sysPipelineItem)],
    ["tools", "Tools", "What the Scientist can call, and where each one runs", (d.tools || []).map(sysToolItem)],
    ["skills", "Skills", "Templates the Scientist fetches when a step needs one", (d.skills || []).map(sysSkillItem)],
  ];
  return `<div class="sys-catalog-head"><h3>Pipelines, tools and skills</h3>` +
    `<input id="sysSearch" type="search" class="preset-search sys-search" autocomplete="off" ` +
    `placeholder="Search names, descriptions, where it runs…" aria-label="Search pipelines, tools and skills" /></div>` +
    lists.map(([key, title, hint, items]) =>
      `<section class="sys-list" data-list="${key}"><h4>${title} <span class="sys-n" data-total="${items.length}">${items.length}</span>` +
      `<span class="sys-hint">${escapeHtml(hint)}</span></h4>` +
      `<div class="sys-items">${items.map(sysItemHtml).join("")}</div>` +
      `<div class="sys-none hidden">Nothing here matches.</div></section>`).join("");
}

function sysCatalogFilter(query) {
  for (const list of document.querySelectorAll("#systemBody .sys-list")) {
    let shown = 0;
    for (const row of list.querySelectorAll(".sys-item")) {
      const hit = sysMatch(query, row.dataset.q || "");
      row.classList.toggle("hidden", !hit);
      shown += hit;
    }
    const n = list.querySelector(".sys-n");
    n.textContent = query.trim() ? `${shown} of ${n.dataset.total}` : n.dataset.total;
    list.querySelector(".sys-items").classList.toggle("hidden", !shown);
    list.querySelector(".sys-none").classList.toggle("hidden", !!shown);
  }
}

// A pipeline's tool opens that tool in the Tools list.
function sysShowTool(name) {
  const box = $("sysSearch");
  box.value = name;
  sysCatalogFilter(name);
  const row = document.querySelector(`#systemBody .sys-item[data-id="tool:${CSS.escape(name)}"]`);
  if (row) { row.open = true; row.scrollIntoView({ behavior: "smooth", block: "center" }); }
}

function wireSysCatalog() {
  const body = $("systemBody");
  body.addEventListener("input", (e) => { if (e.target.id === "sysSearch") sysCatalogFilter(e.target.value); });
  body.addEventListener("keydown", (e) => {
    if (e.target.id === "sysSearch" && e.key === "Escape" && e.target.value) { e.target.value = ""; sysCatalogFilter(""); }
  });
  body.addEventListener("click", (e) => {
    const b = e.target.closest("[data-sys-tool]");
    if (b) sysShowTool(b.dataset.sysTool);
  });
}

// ---- Live workflow graph: the pipeline from /api/system, the running tasks from /api/system/live ----
// The graph is drawn once per /api/system load; the tasks move over it. Its stage nodes are the
// stages the server moves runs through (gateway/run_monitor.py STAGES), its tool nodes the live
// catalog grouped by where each tool runs, so the picture is the code's, not a drawing of it.
const SG = { W: 1040, M: 12, NW: 104, NH: 46, LANE_Y: 74, SIDE_Y0: 140, SIDE_DY: 38, SW: 120, SH: 34,
  AGENT_Y: 268, AW: 116, AH: 36, PLACE_Y: 330, COLW: 128, ROW_H: 22, HEAD: 30, PAD: 8, GAP: 10, BOTTOM: 14 };
const SG_COLORS = ["#ad5a2b", "#1f7a8c", "#6b4fbb", "#2f8a4a", "#c0446b", "#b07d12"];
const SG_ROLE = { plan: "PI", critic: "Critic", scientist: "Scientist", audit: "Claim auditor",
  writer: "Writer", classify: "Router", literature: "Literature", chat: "Chat", reason: "Model" };

// Positions for every node and a path for every edge. Pure: the same graph always lays out the same.
function sysGraphLayout(graph) {
  const nodes = (graph && graph.nodes) || [];
  const byType = (t) => nodes.filter((n) => n.type === t);
  const stages = byType("stage").slice().sort((a, b) => ((a.meta || {}).order || 0) - ((b.meta || {}).order || 0));
  const isMain = (n) => ((n.meta || {}).kinds || []).includes("research");
  const main = stages.filter(isMain), side = stages.filter((n) => !isMain(n));
  const pos = {};
  const step = main.length > 1 ? (SG.W - 2 * SG.M - SG.NW) / (main.length - 1) : 0;
  main.forEach((n, i) => { pos[n.id] = { x: SG.M + SG.NW / 2 + i * step, y: SG.LANE_Y, w: SG.NW, h: SG.NH, kind: "stage" }; });
  const first = main[0] ? pos[main[0].id] : { x: SG.M + SG.NW / 2, y: SG.LANE_Y };
  const last = main.length ? pos[main[main.length - 1].id] : first;
  const sideX = main.length > 3 ? (pos[main[2].id].x + pos[main[3].id].x) / 2 : SG.W / 2;
  side.forEach((n, i) => { pos[n.id] = { x: sideX, y: SG.SIDE_Y0 + i * SG.SIDE_DY, w: SG.SW, h: SG.SH, kind: "side" }; });

  const exec = pos.execute || { x: SG.W / 2 };
  const agentX = { scientist: exec.x, critic: exec.x + 168, model: exec.x - 190 };
  byType("agent").forEach((n, i) => {
    pos[n.id] = { x: agentX[n.id] != null ? agentX[n.id] : SG.M + SG.AW / 2 + i * (SG.AW + 20), y: SG.AGENT_Y, w: SG.AW, h: SG.AH, kind: "agent" };
  });

  // Places as boxes of tool chips, packed into rows and centred; a big box gets more columns.
  const tools = byType("tool");
  const boxes = byType("place").map((p) => {
    const chips = tools.filter((t) => (t.meta || {}).place === p.id);
    const cols = chips.length > 8 ? 3 : chips.length > 4 ? 2 : 1;
    const rows = Math.max(1, Math.ceil(chips.length / cols));
    const inner = cols * SG.COLW + SG.PAD * 2 - 8;
    // Wide enough for its title too (10.5px bold ≈ 6.4px a character), chips centred under it.
    const w = Math.max(inner, Math.ceil(String(p.label || "").length * 6.4 + 22));
    return { p, chips, cols, w, inset: (w - inner) / 2, h: SG.HEAD + rows * SG.ROW_H + SG.PAD };
  });
  const rowsOfBoxes = [];
  let cur = [], width = 0;
  boxes.forEach((b) => {
    const need = (cur.length ? SG.GAP : 0) + b.w;
    if (cur.length && width + need > SG.W - 2 * SG.M) { rowsOfBoxes.push(cur); cur = []; width = 0; }
    width += (cur.length ? SG.GAP : 0) + b.w;
    cur.push(b);
  });
  if (cur.length) rowsOfBoxes.push(cur);
  let y = SG.PLACE_Y;
  rowsOfBoxes.forEach((row) => {
    const total = row.reduce((s, b) => s + b.w, 0) + SG.GAP * (row.length - 1);
    let x = (SG.W - total) / 2;
    const rowH = Math.max(...row.map((b) => b.h));
    row.forEach((b) => {
      pos[b.p.id] = { x: x + b.w / 2, y: y + b.h / 2, w: b.w, h: b.h, kind: "place" };
      b.chips.forEach((t, i) => {
        const c = i % b.cols, r = Math.floor(i / b.cols);
        pos[t.id] = { x: x + b.inset + SG.PAD + c * SG.COLW + (SG.COLW - 8) / 2, y: y + SG.HEAD + r * SG.ROW_H + 9,
          w: SG.COLW - 8, h: 18, kind: "tool" };
      });
      x += b.w + SG.GAP;
    });
    y += rowH + SG.GAP;
  });
  const height = Math.ceil(y - SG.GAP + SG.BOTTOM);

  const edges = [];
  ((graph && graph.edges) || []).forEach((e, i) => {
    const a = pos[e.source], b = pos[e.target];
    if (!a || !b || e.type === "pipeline") return;   // the scRNA order is the chips' own order
    let d = "", lx = (a.x + b.x) / 2, ly = (a.y + b.y) / 2;
    if (e.type === "skip") {
      const top = SG.LANE_Y - SG.NH / 2, x1 = a.x + 14, x2 = b.x - 14, peak = top - 30;
      d = `M${x1},${top} C${x1},${peak} ${x2},${peak} ${x2},${top}`;
      lx = (x1 + x2) / 2; ly = peak + 6;
    } else if (a.kind === "stage" && b.kind === "stage") {
      d = `M${a.x + a.w / 2},${a.y} L${b.x - b.w / 2},${b.y}`;
    } else if (b.kind === "side") {          // request -> a side lane: down, then right
      const r = 10, x0 = a.x, y1 = b.y;
      d = `M${x0},${a.y + a.h / 2} L${x0},${y1 - r} Q${x0},${y1} ${x0 + r},${y1} L${b.x - b.w / 2},${y1}`;
    } else if (a.kind === "side") {          // a side lane -> done: right, then up
      const r = 10, x1 = b.x, y0 = a.y;
      d = `M${a.x + a.w / 2},${y0} L${x1 - r},${y0} Q${x1},${y0} ${x1},${y0 - r} L${x1},${b.y + b.h / 2}`;
    } else if (e.source === "scientist" && e.target === "critic") {
      d = `M${a.x + a.w / 2},${a.y - 6} C${a.x + a.w / 2 + 26},${a.y - 22} ${b.x - b.w / 2 - 26},${b.y - 22} ${b.x - b.w / 2},${b.y - 6}`;
      ly = a.y - 22;
    } else if (e.source === "critic" && e.target === "scientist") {
      d = `M${a.x - a.w / 2},${a.y + 6} C${a.x - a.w / 2 - 26},${a.y + 22} ${b.x + b.w / 2 + 26},${b.y + 22} ${b.x + b.w / 2},${b.y + 6}`;
      ly = a.y + 24;
    } else if (b.kind === "place") {
      const y0 = a.y + a.h / 2, y1 = b.y - b.h / 2;
      d = `M${a.x},${y0} C${a.x},${(y0 + y1) / 2} ${b.x},${(y0 + y1) / 2} ${b.x},${y1}`;
      lx = null;
    } else {
      d = `M${a.x},${a.y + a.h / 2} L${b.x},${b.y - b.h / 2}`;
    }
    edges.push({ id: "e" + i, source: e.source, target: e.target, type: e.type, label: e.label || "", d, lx, ly });
  });
  return { width: SG.W, height, pos, edges, first, last };
}

function _sgAvail(n) { return (n.meta || {}).available === false; }

// The static picture: lanes, agents, places with their tools. Tasks go in the #sgTasks layer.
function sysGraphSvg(graph, layout) {
  const L = layout, P = L.pos, out = [];
  out.push(`<svg class="sg" viewBox="0 0 ${L.width} ${L.height}" role="img" aria-label="Workflow graph with the running tasks">`);
  out.push('<defs><marker id="sgArrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">' +
    '<path d="M0,0 L8,4 L0,8 z" class="sg-arrowhead"/></marker></defs>');
  out.push('<g class="sg-edges">');
  // The side lanes cross the Run steps → Scientist line: drawn last, over a halo, they read as a bridge.
  const isSide = (e) => (P[e.source] || {}).kind === "side" || (P[e.target] || {}).kind === "side";
  [...L.edges.filter((e) => !isSide(e)), ...L.edges.filter(isSide)].forEach((e) => {
    const arrow = e.type === "tool" ? "" : ' marker-end="url(#sgArrow)"';
    if (isSide(e)) out.push(`<path class="sg-edge-halo" d="${e.d}"/>`);
    out.push(`<path class="sg-edge sg-e-${escapeHtml(e.type)}" data-edge="${escapeHtml(e.source)}>${escapeHtml(e.target)}" d="${e.d}"${arrow}/>`);
    if (e.label && e.lx != null) out.push(`<text class="sg-elabel" x="${e.lx}" y="${e.ly}">${escapeHtml(e.label)}</text>`);
  });
  out.push('</g><g class="sg-links" id="sgLinks"></g><g class="sg-nodes">');
  (graph.nodes || []).forEach((n) => {
    const p = P[n.id];
    if (!p) return;
    const m = n.meta || {}, x = p.x - p.w / 2, y = p.y - p.h / 2;
    const attrs = `data-node="${escapeHtml(n.id)}" tabindex="0" role="button" aria-label="${escapeHtml(n.label)}"`;
    if (p.kind === "place") {
      out.push(`<g class="sg-node sg-place" ${attrs}><rect x="${x}" y="${y}" width="${p.w}" height="${p.h}" rx="12"/>` +
        `<text class="sg-place-label" x="${x + 10}" y="${y + 19}">${escapeHtml(n.label)}</text></g>`);
    } else if (p.kind === "tool") {
      out.push(`<g class="sg-node sg-tool${_sgAvail(n) ? " sg-off" : ""}" ${attrs}><rect x="${x}" y="${y}" width="${p.w}" height="${p.h}" rx="6"/>` +
        `<text x="${p.x}" y="${p.y + 3.5}">${escapeHtml(n.label)}</text></g>`);
    } else {
      const sub = p.kind === "agent"
        ? (n.id === "model" ? "every role" : n.id === "scientist" ? `${(m.specialists || []).length} specialists` : "judges each step")
        : (m.roles || []).map((r) => SG_ROLE[r] || r).join(" · ");
      const rx = p.kind === "agent" ? p.h / 2 : 12;
      out.push(`<g class="sg-node sg-${p.kind}" ${attrs}><rect x="${x}" y="${y}" width="${p.w}" height="${p.h}" rx="${rx}"/>` +
        `<text class="sg-label" x="${p.x}" y="${p.y + (sub ? -2 : 4)}">${escapeHtml(n.label)}</text>` +
        (sub ? `<text class="sg-sub" x="${p.x}" y="${p.y + 12}">${escapeHtml(sub)}</text>` : "") + "</g>");
    }
  });
  out.push('</g><g class="sg-tasks" id="sgTasks"></g></svg>');
  return out.join("");
}

const SG_PER_NODE = 4;          // dots drawn on one node; the rest are a "+n"
const SG_DONE_LINGER = 90;      // seconds a finished run's dot stays on Done (its card stays 10 minutes)

// Where each run's markers go, and what lights up. Pure (no DOM), so it is tested under node.
// ``now`` is the server's clock in seconds. Returns { tokens: [{key, run, node, x, y, color, cls, title}],
// more: [{node, count, x, y}], hot: {node: "hot"|"waiting"}, links: [...], flows: [...] }.
function sysTaskTargets(graph, layout, runs, now) {
  const P = layout.pos, has = (id) => !!P[id];
  const tokens = [], hot = {}, links = [], flows = [], more = {};
  const atNode = {};
  const place = (key, run, nodeId, cls, title) => {
    const p = P[nodeId];
    if (!p) return;
    if (!run.ended) hot[nodeId] = hot[nodeId] === "waiting" || cls.includes("waiting") ? "waiting" : "hot";
    const k = atNode[nodeId] = (atNode[nodeId] || 0) + 1;
    if (k > SG_PER_NODE) { more[nodeId] = (more[nodeId] || 0) + 1; return; }
    const small = p.kind === "tool";
    const x = p.x + p.w / 2 - (small ? 9 : 12) - (k - 1) * (small ? 15 : 19);
    const y = p.y - p.h / 2 + (small ? 1 : 0);
    tokens.push({ key, run: run.ordinal, node: nodeId, x, y, color: run.color, cls, title, small });
  };
  (runs || []).forEach((r) => {
    const label = r.mine ? (r.question || "Your run") : (r.owner ? `${r.owner}'s run` : "Another account's run");
    if (r.ended) {
      // A finished run arrives at Done and leaves it after a while, without lighting it.
      if (now == null || now - r.ended <= SG_DONE_LINGER)
        place(r.key, r, has("done") ? "done" : r.stage, `sg-tok-ended sg-out-${r.outcome || "ended"}`,
          `${label} — ${r.outcome || "ended"}`);
      return;
    }
    const lane = r.kind === "research" ? null : r.kind;           // literature / chat / corpus
    // A literature run waits for its plan's review on the main line first; its lane once approved.
    const home = lane && has(lane) && (lane !== "literature" || r.stage === "literature") ? lane : null;
    const active = r.active || [];
    if (active.length && ["execute", "literature", "chat", "corpus"].includes(r.stage)) {
      active.forEach((a, i) => {
        const key = i === 0 ? r.key : `${r.key}:${a.n || i}`;
        const stepTxt = a.step ? `Step ${a.n ? a.n + ": " : ""}${a.step}` : "";
        let nodeId = home || "scientist";
        if (a.phase === "tool" && has("tool:" + a.tool)) nodeId = "tool:" + a.tool;
        else if (a.phase === "critic" && has("critic")) nodeId = "critic";
        const what = a.phase === "tool" ? `running ${a.tool}` : a.phase === "critic" ? "the Critic is judging it" : "the model is choosing the next action";
        place(key, r, nodeId, `sg-tok-${a.phase}`, [label, stepTxt, what].filter(Boolean).join(" — "));
        if (a.phase === "tool" && nodeId.startsWith("tool:")) links.push({ from: home || "scientist", to: nodeId, color: r.color, cls: "sg-link-tool" });
        if (a.phase === "model" && has("model")) links.push({ from: "model", to: nodeId, color: r.color, cls: "sg-link-model" });
      });
    } else {
      const nodeId = home || (has(r.stage) ? r.stage : "request");
      place(r.key, r, nodeId, r.waiting ? "sg-tok-waiting" : "sg-tok-stage",
        `${label} — ${r.stage_label || r.stage}${r.waiting ? " (waiting for you)" : ""}`);
      if ((r.llm && (r.llm.thinking || []).length) && has("model")) links.push({ from: "model", to: nodeId, color: r.color, cls: "sg-link-model" });
    }
    // The lane edge the run came in on keeps flowing while it is in that stage.
    const into = (layout.edges || []).find((e) => e.target === (home || r.stage) && e.type === "flow");
    if (into) flows.push(`${into.source}>${into.target}`);
  });
  const extra = Object.entries(more).map(([node, count]) => {
    const p = P[node], small = p.kind === "tool";
    return { node, count, x: p.x + p.w / 2 - (small ? 9 : 12) - SG_PER_NODE * (small ? 15 : 19), y: p.y - p.h / 2 };
  });
  return { tokens, more: extra, hot, links, flows };
}

function _sgLinkPath(P, from, to) {
  const a = P[from], b = P[to];
  if (!a || !b) return "";
  const y0 = a.y + (b.y > a.y ? a.h / 2 : -a.h / 2), y1 = b.y + (b.y > a.y ? -b.h / 2 : b.h / 2);
  const x0 = a.x + (b.x - a.x) * 0.15, x1 = b.x;
  return `M${x0},${y0} C${x0},${(y0 + y1) / 2} ${x1},${(y0 + y1) / 2} ${x1},${y1}`;
}

const sysLive = { timer: null, graph: null, layout: null, runs: [], now: null, selected: null, failed: 0, node: null };

function renderWorkflowGraph(graph) {
  const host = $("systemGraph");
  if (!host) return;
  if (!graph || !(graph.nodes || []).length) { host.innerHTML = '<div class="empty-hint">No workflow graph.</div>'; return; }
  sysLive.graph = graph;
  sysLive.layout = sysGraphLayout(graph);
  host.innerHTML = sysGraphSvg(graph, sysLive.layout);
  host.onclick = (ev) => {
    const g = ev.target.closest("[data-node]");
    if (g) { sysLive.node = g.dataset.node; showGraphNodeInfo(g.dataset.node); }
  };
  host.onkeydown = (ev) => {
    const g = ev.target.closest && ev.target.closest("[data-node]");
    if (g && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); sysLive.node = g.dataset.node; showGraphNodeInfo(g.dataset.node); }
  };
  paintSystemTasks();
}

// Colour and number each run once, so a run keeps its marker while others come and go.
const _sgOrdinals = {};
let _sgNextOrdinal = 1;
function _sgDecorate(runs) {
  return (runs || []).map((r) => {
    if (!_sgOrdinals[r.key]) _sgOrdinals[r.key] = _sgNextOrdinal++;
    const ordinal = _sgOrdinals[r.key];
    return { ...r, ordinal, color: SG_COLORS[(ordinal - 1) % SG_COLORS.length] };
  });
}

function paintSystemTasks() {
  const L = sysLive.layout, svg = document.querySelector("#systemGraph svg.sg");
  if (!L || !svg) return;
  const t = sysTaskTargets(sysLive.graph, L, sysLive.runs, sysLive.now);
  svg.querySelectorAll(".sg-node").forEach((g) => {
    const h = t.hot[g.dataset.node];
    g.classList.toggle("is-hot", h === "hot");
    g.classList.toggle("is-waiting", h === "waiting");
  });
  const flowing = new Set(t.flows);
  svg.querySelectorAll(".sg-edge").forEach((p) => p.classList.toggle("is-flowing", flowing.has(p.dataset.edge)));
  $("sgLinks").innerHTML = t.links.map((l) =>
    `<path class="sg-link ${l.cls}" d="${_sgLinkPath(L.pos, l.from, l.to)}" style="stroke:${l.color}"/>`).join("");
  // Markers are kept by key and moved, so a step travelling from the Scientist to a tool to the
  // Critic glides there instead of blinking.
  const layer = $("sgTasks"), seen = new Set();
  t.tokens.forEach((k) => {
    seen.add(k.key);
    let g = layer.querySelector(`[data-key="${CSS.escape(k.key)}"]`);
    if (!g) {
      g = document.createElementNS("http://www.w3.org/2000/svg", "g");
      g.setAttribute("data-key", k.key);
      g.innerHTML = '<circle class="sg-tok-halo" r="13"/><circle class="sg-tok-dot" r="9"/><text class="sg-tok-num" y="3.5"></text><title></title>';
      const start = L.pos.request || L.first;
      g.style.transform = `translate(${start.x}px, ${start.y - SG.NH / 2}px)`;
      layer.appendChild(g);
      g.getBoundingClientRect();     // commit the start position so the move below animates
    }
    g.setAttribute("class", `sg-token ${k.cls}${k.small ? " sg-tok-small" : ""}${sysLive.selected === k.key.split(":")[0] ? " is-selected" : ""}`);
    g.style.transform = `translate(${k.x}px, ${k.y}px)`;
    g.style.setProperty("--tok", k.color);
    g.querySelector(".sg-tok-dot").setAttribute("r", k.small ? "7.5" : "9");
    g.querySelector(".sg-tok-num").textContent = String(k.run);
    g.querySelector("title").textContent = k.title;
  });
  layer.querySelectorAll(".sg-token").forEach((g) => { if (!seen.has(g.dataset.key)) g.remove(); });
  layer.querySelectorAll(".sg-more").forEach((g) => g.remove());
  t.more.forEach((m) => layer.insertAdjacentHTML("beforeend",
    `<text class="sg-more" x="${m.x}" y="${m.y + 4}">+${m.count}</text>`));
}

function _sgAgo(sec) {
  if (sec < 60) return `${Math.max(0, Math.round(sec))}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${String(Math.floor(sec % 60)).padStart(2, "0")}s`;
  return `${Math.floor(sec / 3600)}h ${String(Math.floor((sec % 3600) / 60)).padStart(2, "0")}m`;
}

const SG_KIND = { research: "Research", literature: "Literature", chat: "Chat", corpus: "RetiGene scan" };
const SG_OUTCOME = { finished: "Finished", stopped: "Stopped", cancelled: "Cancelled", failed: "Failed", ended: "Ended" };

// One card in "Running now". ``now`` is the server's clock (seconds), so elapsed times are the
// server's, whatever the viewer's clock says.
function sysRunCardHtml(r, now) {
  const who = r.mine ? "" : `<span class="sr-who">${escapeHtml(r.owner ? r.owner : "another account")}</span>`;
  const head = `<span class="sr-dot" style="background:${r.color}">${r.ordinal}</span>` +
    `<span class="sr-kind">${escapeHtml(SG_KIND[r.kind] || r.kind)}</span>${who}` +
    `<span class="sr-stage${r.waiting ? " waiting" : ""}">${escapeHtml(r.ended ? (SG_OUTCOME[r.outcome] || "Ended") : r.stage_label || r.stage)}</span>` +
    `<span class="sr-time">${r.ended ? _sgAgo(now - r.ended) + " ago" : _sgAgo(now - r.started)}</span>`;
  const q = r.question ? `<div class="sr-q" title="${escapeHtml(r.question)}">${escapeHtml(r.question)}</div>` : "";
  const lines = [];
  if (!r.ended) {
    const whom = r.mine ? "you" : "its owner";
    if (r.waiting === "plan") lines.push(`Waiting for ${whom} to review the plan.`);
    else if (r.waiting === "confirm") lines.push(`Waiting for ${whom} to confirm an action.`);
    (r.active || []).forEach((a) => {
      const what = a.phase === "tool" ? `<code>${escapeHtml(a.tool)}</code>` : a.phase === "critic" ? "Critic judging" : "model thinking";
      const step = a.n ? `Step ${a.n}` : a.step ? "Step" : "";
      const spec = a.specialist && a.specialist !== "Scientist" ? ` · ${escapeHtml(a.specialist)}` : "";
      lines.push(`${step ? `<b>${step}</b>${spec} · ` : ""}${what} <span class="muted">${_sgAgo(now - a.since)}</span>` +
        (a.step ? `<div class="sr-step" title="${escapeHtml(a.step)}">${escapeHtml(a.step)}</div>` : ""));
    });
    if (!(r.active || []).length && r.llm && (r.llm.thinking || []).length)
      lines.push(`${escapeHtml(r.llm.thinking.map((x) => SG_ROLE[x] || x).join(", "))} thinking <span class="muted">${_sgAgo(now - r.stage_since)}</span>`);
  }
  const s = r.steps || {};
  const bar = s.total ? `<div class="sr-bar" title="${s.accepted} of ${s.total} steps accepted"><span style="width:${Math.min(100, Math.round(100 * s.accepted / s.total))}%;background:${r.color}"></span></div>` +
    `<div class="sr-meta">${s.accepted}/${s.total} steps accepted · ${r.tools_called || 0} tool call${r.tools_called === 1 ? "" : "s"}${r.tool_errors ? ` · ${r.tool_errors} failed` : ""}</div>` : "";
  const last = (r.recent || []).slice(-1)[0];
  const note = last && !r.ended ? `<div class="sr-note">${escapeHtml(last.text)}</div>` : "";
  return `<div class="sr-card${r.ended ? " ended" : ""}${sysLive.selected === r.key ? " selected" : ""}" data-run="${escapeHtml(r.key)}" tabindex="0">` +
    `<div class="sr-head">${head}</div>${q}${lines.map((l) => `<div class="sr-line">${l}</div>`).join("")}${bar}${note}</div>`;
}

function paintSystemRuns(data) {
  const el = $("systemRuns");
  if (!el) return;
  const now = (data && data.now) || Date.now() / 1000;
  const runs = sysLive.runs;
  const live = runs.filter((r) => !r.ended), ended = runs.filter((r) => r.ended).slice(0, 6);
  const head = `<div class="sr-title">Running now <span class="muted">${live.length ? live.length : "nothing"}` +
    `${data && data.sessions != null ? ` · ${data.sessions} session${data.sessions === 1 ? "" : "s"} open` : ""}</span></div>`;
  el.innerHTML = head + (live.length ? live.map((r) => sysRunCardHtml(r, now)).join("")
    : '<div class="empty-hint sr-empty">No task is running. Start one in Research and watch it move through the graph.</div>') +
    (ended.length ? `<div class="sr-sub">Finished in the last 10 minutes</div>${ended.map((r) => sysRunCardHtml(r, now)).join("")}` : "");
}

async function pollSystemLive() {
  if (document.hidden || !$("view-system").classList.contains("active")) return;
  try {
    const res = await fetch("/api/system/live", { cache: "no-store" });
    if (res.status === 401) { $("systemRuns").innerHTML = '<div class="empty-hint">Sign in to see the running tasks.</div>'; return; }
    const data = await res.json();
    sysLive.failed = 0;
    sysLive.runs = _sgDecorate(data.runs || []);
    sysLive.now = data.now;
    paintSystemTasks();
    paintSystemRuns(data);
    if (sysLive.node) showGraphNodeInfo(sysLive.node);
  } catch {
    if (++sysLive.failed === 3) $("systemRuns").innerHTML = '<div class="empty-hint">Lost contact with the server — retrying.</div>';
  }
}

function startSystemLive() {
  stopSystemLive();
  pollSystemLive();
  sysLive.timer = setInterval(pollSystemLive, 1500);
}
function stopSystemLive() {
  if (sysLive.timer) { clearInterval(sysLive.timer); sysLive.timer = null; }
}

function showGraphNodeInfo(id) {
  const box = $("systemGraphInfo");
  const n = sysLive.graph && (sysLive.graph.nodes || []).find((x) => x.id === id);
  if (!box || !n) return;
  const m = n.meta || {};
  const type = { stage: "stage", agent: "agent", place: "where tools run", tool: "tool" }[n.type] || n.type;
  const rows = [`<div class="gi-title">${escapeHtml(n.label)} <span class="gi-type">${escapeHtml(type)}</span></div>`];
  if (m.kinds) rows.push(`<div><span class="gi-k">runs</span> ${m.kinds.map((k) => escapeHtml(SG_KIND[k] || k)).join(", ")}</div>`);
  if (m.category) rows.push(`<div><span class="gi-k">category</span> ${escapeHtml(m.category)}</div>`);
  if (m.place) {
    const pl = (sysLive.graph.nodes || []).find((x) => x.id === m.place);
    rows.push(`<div><span class="gi-k">runs on</span> ${escapeHtml(pl ? pl.label : m.runs_on)}</div>`);
  }
  if (m.where) rows.push(`<div><span class="gi-k">code</span> <code>${escapeHtml(m.where)}</code></div>`);
  if (typeof m.available === "boolean") rows.push(`<div><span class="gi-k">here</span> <span class="cap ${m.available ? "on" : "off"}">${m.available ? "available" : "unavailable"}</span></div>`);
  if (m.reads_private_data) rows.push('<div><span class="tag-priv">reads private data</span></div>');
  if (m.requires && m.requires.length) rows.push(`<div><span class="gi-k">needs</span> ${m.requires.map(escapeHtml).join(", ")}</div>`);
  if (m.description) rows.push(`<div class="gi-desc">${escapeHtml(m.description)}</div>`);
  if (m.roles && m.roles.length && typeof m.roles[0] === "object")
    rows.push(`<div class="gi-list">${m.roles.map((r) => `<div><b>${escapeHtml(SG_ROLE[r.role] || r.role)}</b> — ${escapeHtml(r.does)}</div>`).join("")}</div>`);
  if (m.specialists && m.specialists.length)
    rows.push(`<div class="gi-list">${m.specialists.map((s) => `<div><b>${escapeHtml(s.name)}</b></div>`).join("")}</div>`);
  if (m.pipelines && m.pipelines.length)
    rows.push(`<div class="gi-list"><div class="gi-k">pipelines it can follow</div>${m.pipelines.map((p) => `<div>${escapeHtml(p.label)}</div>`).join("")}</div>`);
  if (n.type === "place") {
    const tools = (sysLive.graph.nodes || []).filter((x) => x.type === "tool" && (x.meta || {}).place === n.id);
    rows.push(`<div class="gi-list">${tools.map((t) => `<div><code>${escapeHtml(t.label)}</code></div>`).join("")}</div>`);
  }
  const here = sysTaskTargets(sysLive.graph, sysLive.layout, sysLive.runs, sysLive.now).tokens.filter((k) => k.node === id);
  if (here.length) rows.push(`<div class="gi-here"><div class="gi-k">here now</div>${here.map((k) =>
    `<div><span class="sr-dot small" style="background:${k.color}">${k.run}</span> ${escapeHtml(k.title)}</div>`).join("")}</div>`);
  box.innerHTML = rows.join("");
}

function onSystemRunsClick(e) {
  const card = e.target.closest("[data-run]");
  if (!card) return;
  sysLive.selected = sysLive.selected === card.dataset.run ? null : card.dataset.run;
  paintSystemTasks();
  document.querySelectorAll("#systemRuns .sr-card").forEach((c) => c.classList.toggle("selected", c.dataset.run === sysLive.selected));
}

// ---- Datasets view ----
async function loadDatasets() {
  renderUploads();   // show any in-flight/failed uploads at the top of the tab
  const el = $("datasetsList"); el.innerHTML = '<div class="empty-hint">Loading…</div>';
  try {
    const data = await (await fetch("/api/datasets")).json();
    if (Number.isInteger(data.max_bound_datasets)) state.maxBoundDatasets = data.max_bound_datasets;
    const items = data.datasets || [];
    for (const x of items) {
      const known = state.datasets.find((d) => d.id === x.id || d.path === x.path);
      if (known) Object.assign(known, x); else state.datasets.push(x);
    }
    const available = new Set(items.map((x) => String(x.id)));
    for (const id of state.selectedDatasetIds) if (!available.has(id)) state.selectedDatasetIds.delete(id);
    renderDatasetSelectionCount();
    el.innerHTML = items.length ? items.map((x) => {
      const from = chatLabel(x.conversation_id);
      const fromHtml = !from ? ""
        : from.open ? ` · <button type="button" class="upload-chat" data-open-chat="${escapeHtml(String(x.conversation_id))}" title="Open the chat this upload was started from">${escapeHtml(from.text)}</button>`
        : ` · ${escapeHtml(from.text)}`;
      const q = sysSearchText([x.name, x.kind, x.location === "hpc3" ? "on hpc3" : "on the server", from && from.text,
                               fmtDate(x.uploaded_at), (x.uploaded_at || "").slice(0, 10)]);
      return `<div class="card" data-q="${escapeHtml(q)}"><input type="checkbox" data-select-dataset="${x.id}" aria-label="Select ${escapeHtml(x.name)}"${state.selectedDatasetIds.has(String(x.id)) ? " checked" : ""} /><div class="card-main"><strong>${icon(datasetIcon(x.kind))} ${escapeHtml(x.name)}${isFolderKind(x.kind) ? "/" : ""}</strong>` +
      `<span class="muted">${escapeHtml(x.kind)} · ${fmtSize(x.size_bytes)} · ${fmtDate(x.uploaded_at)}${fromHtml}${x.location === "hpc3" ? ` · <span title="Kept only on HPC3 so far: it is moved to the server when you connect">on HPC3</span>` : ""}${metadataBadge(x.metadata)}</span></div>` +
      `<button class="ghost small" data-preview="${x.id}" data-name="${escapeHtml(x.name)}" title="See what this dataset holds">Preview</button>` +
      `<button class="ghost small" data-meta="${x.id}" title="Review how this dataset is described (Croissant metadata)">Metadata</button>` +
      `<a class="ghost small" href="/api/datasets/${x.id}/download" download data-dl-size="${Number(x.size_bytes) || 0}" data-name="${escapeHtml(x.name)}" title="${isFolderKind(x.kind) ? "Download the folder as one .zip" : "Download the file"}">Download</a>` +
      (state.boundPaths.includes(x.path) ? `<span class="muted">Attached</span>`
        : `<button class="ghost small" data-use="${escapeHtml(x.path)}" data-name="${escapeHtml(x.name)}" data-kind="${escapeHtml(x.kind)}">Use</button>`) +
      `<button class="ghost small danger" data-del="${x.id}" data-path="${escapeHtml(x.path)}" data-name="${escapeHtml(x.name)}" data-kind="${escapeHtml(x.kind)}" data-location="${escapeHtml(x.location || "server")}">${x.kind.startsWith("server-") ? "Remove" : "Delete"}</button></div>`;
    }).join("")
      : '<div class="empty-hint">No datasets yet — upload one in Research.</div>';
  } catch { el.innerHTML = '<div class="empty-hint">Failed to load.</div>'; }
  filterCards("datasetsList", "datasetsCount", $("datasetsSearch").value);   // a reload keeps the search
  loadMcpTokens();
}

// ---- Files view: two tabs, Datasets (above) and Outputs (what the runs wrote, below) ----
let filesTab = "datasets";
function switchFilesTab(tab) {
  filesTab = tab === "outputs" ? "outputs" : "datasets";
  document.querySelectorAll(".files-tab").forEach((b) => {
    const on = b.dataset.filesTab === filesTab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  document.querySelectorAll(".files-pane").forEach((p) => p.classList.toggle("hidden", p.id !== "filesPane-" + filesTab));
  if (filesTab === "outputs") loadOutputs();
}
function loadFiles() {
  loadDatasets();
  if (filesTab === "outputs") loadOutputs();
}

// The search over a page's list: each card's data-q holds what it is found by (see sysSearchText), and
// every term must match. Beside the box, "n of N" while a search is on.
function filterCards(listId, countId, query) {
  const list = $(listId);
  if (!list) return;
  const cards = [...list.children].filter((c) => c.dataset && c.dataset.q !== undefined);
  let shown = 0;
  for (const c of cards) {
    const hit = sysMatch(query, c.dataset.q || "");
    c.classList.toggle("hidden", !hit);
    shown += hit;
  }
  const searching = !!String(query || "").trim() && cards.length > 0;
  $(countId).textContent = searching ? `${shown} of ${cards.length}` : "";
  let none = list.querySelector(":scope > .list-none");
  if (!none) {
    none = document.createElement("div");
    none.className = "empty-hint list-none hidden";
    none.textContent = "Nothing here matches.";
    list.appendChild(none);
  }
  none.classList.toggle("hidden", !(searching && !shown));
}
function wireListSearch(boxId, listId, countId) {
  const box = $(boxId);
  box.addEventListener("input", () => filterCards(listId, countId, box.value));
  box.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && box.value) { box.value = ""; filterCards(listId, countId, ""); }
  });
}
async function onDatasetsClick(e) {
  const selected = e.target.closest("[data-select-dataset]");
  if (selected) {
    if (selected.checked) state.selectedDatasetIds.add(selected.dataset.selectDataset);
    else state.selectedDatasetIds.delete(selected.dataset.selectDataset);
    renderDatasetSelectionCount();
    return;
  }
  const open = e.target.closest("[data-open-chat]");
  if (open) { openChat(open.dataset.openChat); return; }
  const pv = e.target.closest("[data-preview]");
  if (pv) { openDatasetPreview(`/api/datasets/${Number(pv.dataset.preview)}`, pv.dataset.name || ""); return; }
  const dl = e.target.closest("[data-dl-size]");
  if (dl) { if (!confirmLargeDownload(Number(dl.dataset.dlSize), dl.dataset.name)) e.preventDefault(); return; }
  const meta = e.target.closest("[data-meta]");
  if (meta) { openMetadata(meta.dataset.meta); return; }
  const b = e.target.closest("[data-use]");
  if (b) {
    addDataset({ name: b.dataset.name || b.dataset.use.split("/").pop(), path: b.dataset.use, kind: b.dataset.kind || "file" });
    switchView("research");
    toast("Dataset loaded into Research.");
    return;
  }
  const d = e.target.closest("[data-del]");
  if (d && await deleteDatasetRow(d)) loadDatasets();
}

// Delete the dataset a button names (data-del, -name, -kind, -location, -path), after asking; true
// when it is gone. Files → Datasets and My account → Storage both use it.
async function deleteDatasetRow(d) {
  const fromServer = (d.dataset.kind || "").startsWith("server-");
  const ask = fromServer
    ? `Remove "${d.dataset.name}" from your datasets?\n\nThe data on the server is not touched. Your HPC3 copy of it is deleted (a later run copies it again).`
    : d.dataset.location === "hpc3"
    ? `Delete dataset "${d.dataset.name}" from your HPC3 storage?\n\nThis removes its files there and its history. This cannot be undone.`
    : `Delete dataset "${d.dataset.name}" from the server?\n\nThis removes its files on the server (and this session's HPC3 copy) and its history. This cannot be undone.`;
  if (!confirm(ask)) return false;
  try {
    const res = await fetch("/api/datasets/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: Number(d.dataset.del) }) });
    const j = await res.json();
    if (!res.ok) { toast(j.error || "Delete failed"); return false; }
    if (d.dataset.path) unbindEverywhere(d.dataset.path);
    toast("Dataset deleted.");
    return true;
  } catch (err) { toast(isUnreachable(err) ? "Can't reach the local server." : "Delete failed: " + err.message); }
  return false;
}

// ---- Dataset metadata (Croissant; RCHENLAB/AiScientist issue 9) ----
// The review form edits only what cannot be measured. Each value the owner has not set says where
// it came from and how sure the drafting was; the ones to check first are marked.
const MD_SOURCE_LABEL = {
  "owner": "yours", "upload-check": "measured at upload", "computed": "measured", "run-record": "from the run",
  "account": "from your account", "registry": "from the registry", "file-content": "drafted from the file's content",
  "file-name": "drafted from the file name", "default": "placeholder",
};
let mdVocab = null;            // a promise of /api/metadata/vocabulary
let mdCurrent = null;          // { ref, view } of the open dialog

function metadataBadge(m) {
  if (!m) return "";
  if (m.valid === false) return ' · <span class="md-badge md-bad" title="Its croissant.json did not validate">metadata invalid</span>';
  return m.status === "reviewed"
    ? ' · <span class="md-badge md-ok">metadata reviewed</span>'
    : ' · <span class="md-badge md-draft" title="Drafted automatically — review it">metadata draft</span>';
}

function mdSourceHtml(src) {
  if (!src || !src.source) return "";
  const label = MD_SOURCE_LABEL[src.source] || src.source;
  if (src.reviewed) return `<span class="md-src md-src-owner">${escapeHtml(label)}</span>`;
  const conf = typeof src.confidence === "number" ? ` · ${Math.round(src.confidence * 100)}% sure` : "";
  const low = typeof src.confidence === "number" && src.confidence < 0.7;
  return `<span class="md-src ${low ? "md-src-low" : ""}" title="${escapeHtml(src.detail || "")}">${escapeHtml(label + conf)}${low ? " — check" : ""}</span>`;
}

function mdFieldHtml(f, value, src, vocab) {
  const id = `mdf_${f.key}`;
  const head = `<span class="md-field-head">${escapeHtml(f.label)} ${mdSourceHtml(src)}</span>`;
  const hint = f.hint ? `<span class="hint-inline md-hint-line">${escapeHtml(f.hint)}</span>` : "";
  const show = f.show_if ? ` data-show-if="${escapeHtml(f.show_if)}"` : "";
  if (f.type === "bool") {
    return `<label class="check"${show}><input id="${id}" name="${f.key}" type="checkbox" ${value ? "checked" : ""}/> ${escapeHtml(f.label)}</label>`;
  }
  if (f.type === "textarea") {
    return `<label${show}>${head}<textarea id="${id}" name="${f.key}" rows="3">${escapeHtml(value || "")}</textarea>${hint}</label>`;
  }
  if (f.type === "select") {
    const opts = (vocab.license || []).map((o) =>
      `<option value="${escapeHtml(o.id)}" ${o.id === value ? "selected" : ""}>${escapeHtml(o.label)}</option>`).join("");
    return `<label${show}>${head}<select id="${id}" name="${f.key}"><option value="" ${value ? "" : "selected"}>— not chosen (private) —</option>${opts}</select>${hint}</label>`;
  }
  const text = Array.isArray(value) ? value.join(", ") : (value || "");
  const list = f.type === "terms" ? ` list="${id}_list"` : "";
  const datalist = f.type === "terms"
    ? `<datalist id="${id}_list">${(vocab[f.vocab] || []).map((t) => `<option value="${escapeHtml(t.label)}">${escapeHtml(t.id)}</option>`).join("")}</datalist>` : "";
  return `<label${show}>${head}<input id="${id}" name="${f.key}" type="text" value="${escapeHtml(text)}"${list} autocomplete="off" />${datalist}${hint}</label>`;
}

function mdSyncShowIf() {
  const human = $("mdf_human_data");
  document.querySelectorAll("#metadataForm [data-show-if]").forEach((el) => {
    el.classList.toggle("hidden", !(human && human.checked));
  });
}

function renderMetadata(view, vocab) {
  const form = view.form || { fields: [], values: {}, sources: {}, measured: {}, needs_review: [] };
  const m = form.measured || {};
  const files = m.file_count != null ? `${m.file_count} file${m.file_count === 1 ? "" : "s"}` : "files not listed";
  const sums = m.files_listed ? ` · ${m.files_with_sha256} of ${m.files_listed} with a SHA-256 checksum` : "";
  const tables = (m.record_sets || []).length
    ? ` · tables: ${(m.record_sets || []).map((r) => `${escapeHtml(r.name)} (${r.fields} columns)`).join(", ")}` : "";
  $("metadataTitle").textContent = view.name || "Dataset metadata";
  $("metadataMeasured").innerHTML = `Measured: ${files} · ${fmtSize(m.size_bytes)}${sums}${tables}. ` +
    `ID <code>${escapeHtml(view.uuid)}</code>`;
  const todo = form.needs_review || [];
  $("metadataHint").innerHTML = view.status === "reviewed" && !todo.length
    ? "You have reviewed this description."
    : `Drafted automatically. ${todo.length ? `Check first: <strong>${todo.map((k) => escapeHtml(((form.fields || []).find((f) => f.key === k) || {}).label || k)).join(", ")}</strong>. ` : ""}` +
      "Never enter a patient's name or identifier, or a path on the server.";
  $("metadataForm").innerHTML = (form.fields || []).map((f) => mdFieldHtml(f, form.values[f.key], form.sources[f.key], vocab)).join("");
  mdSyncShowIf();
  const v = view.validation || {};
  $("metadataValidation").innerHTML = v.ok
    ? `${icon("verified")} Valid Croissant 1.1 (checked by ${escapeHtml(v.validator || "")}${(v.warnings || []).length ? `; ${v.warnings.length} warning(s): ${escapeHtml(v.warnings.join("; "))}` : ""}).`
    : `<span class="md-badge md-bad">Not valid</span> ${escapeHtml((v.errors || []).join("; "))}`;
  $("metadataDownload").href = `/api/datasets/${encodeURIComponent(view.uuid)}/croissant.json`;
}

async function openMetadata(ref) {
  $("metadataError").classList.add("hidden");
  $("metadataForm").innerHTML = '<div class="storage-loading">Loading…</div>';
  $("metadataMeasured").textContent = ""; $("metadataHint").textContent = ""; $("metadataValidation").textContent = "";
  $("metadataModal").classList.remove("hidden");
  try {
    if (!mdVocab) mdVocab = fetch("/api/metadata/vocabulary").then((r) => r.json()).catch(() => { mdVocab = null; return {}; });
    const res = await fetch(`/api/datasets/${encodeURIComponent(ref)}/metadata`);
    const view = await res.json();
    if (!res.ok) throw new Error(view.detail || "not found");
    mdCurrent = { ref, view };
    renderMetadata(view, await mdVocab);
  } catch (err) {
    $("metadataForm").innerHTML = "";
    $("metadataError").textContent = isUnreachable(err) ? "Can't reach the server." : "Could not load the metadata: " + err.message;
    $("metadataError").classList.remove("hidden");
  }
}

async function saveMetadata(e) {
  e.preventDefault();
  if (!mdCurrent) return;
  const out = {};
  for (const f of (mdCurrent.view.form || {}).fields || []) {
    const el = $(`mdf_${f.key}`);
    if (!el) continue;
    out[f.key] = f.type === "bool" ? el.checked : el.value;
  }
  $("metadataSave").disabled = true;
  $("metadataError").classList.add("hidden");
  try {
    const res = await fetch(`/api/datasets/${encodeURIComponent(mdCurrent.ref)}/metadata`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(out) });
    const view = await res.json();
    if (!res.ok) throw new Error(view.detail || "save failed");
    mdCurrent.view = view;
    renderMetadata(view, await (mdVocab || {}));
    toast("Metadata saved.");
    loadDatasets();
  } catch (err) {
    $("metadataError").textContent = isUnreachable(err) ? "Can't reach the server." : err.message;
    $("metadataError").classList.remove("hidden");
  } finally { $("metadataSave").disabled = false; }
}

// Personal access tokens for MCP clients.
async function loadMcpTokens() {
  const box = $("mcpSection");
  if (!box) return;
  try {
    const res = await fetch("/api/account/tokens");
    if (!res.ok) { box.classList.add("hidden"); return; }
    const tokens = (await res.json()).tokens || [];
    box.classList.remove("hidden");
    $("mcpTokens").innerHTML = tokens.length ? tokens.map((t) =>
      `<div class="storage-item"><span class="si-name">${icon("key")} ${escapeHtml(t.name)} <code>${escapeHtml(t.prefix)}…</code></span>` +
      `<span class="si-size">${t.last_used_at ? "used " + fmtDate(t.last_used_at) : "never used"}</span>` +
      `<button type="button" class="si-del" data-revoke-token="${t.id}">Revoke</button></div>`).join("")
      : '<div class="storage-note">No tokens yet.</div>';
  } catch { box.classList.add("hidden"); }
}

async function createMcpToken(e) {
  e.preventDefault();
  try {
    const res = await fetch("/api/account/tokens", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("mcpTokenName").value.trim() || "MCP client" }) });
    const j = await res.json();
    if (!res.ok) { toast(j.detail || "Could not create a token."); return; }
    $("mcpTokenName").value = "";
    const url = `${location.origin}/mcp`;
    const shown = $("mcpTokenShown");
    shown.innerHTML = `<p><strong>Copy this token now:</strong> it is shown only once.</p>` +
      `<pre class="md-code">${escapeHtml(j.token)}</pre>` +
      `<p class="storage-note">Claude Code:</p><pre class="md-code">claude mcp add --transport http aiscientist ${escapeHtml(url)} --header "Authorization: Bearer ${escapeHtml(j.token)}"</pre>` +
      `<p class="storage-note">Any MCP client (Streamable HTTP): URL <code>${escapeHtml(url)}</code>, header <code>Authorization: Bearer &lt;token&gt;</code>.</p>`;
    shown.classList.remove("hidden");
    loadMcpTokens();
  } catch (err) { toast(isUnreachable(err) ? "Can't reach the server." : "Could not create a token."); }
}

async function onMcpTokensClick(e) {
  const b = e.target.closest("[data-revoke-token]");
  if (!b || !confirm("Revoke this token? A client using it stops working at once.")) return;
  const res = await fetch(`/api/account/tokens/${encodeURIComponent(b.dataset.revokeToken)}`, { method: "DELETE" });
  toast(res.ok ? "Token revoked." : "Could not revoke it.");
  $("mcpTokenShown").classList.add("hidden");
  loadMcpTokens();
}

// ---- Dataset preview (RCHENLAB/AiScientist issue 10) ----
// What a dataset holds, read on the server where it is kept: an .h5ad's cells, genes, metadata
// columns and first rows; a table's first rows; a VCF's header and first records; a folder's files.
// The server reads only a bounded part, so a 15 GB matrix opens as fast as a small one. The same viewer
// opens a run's outputs (Files → Outputs): ``base`` is the API path of what is shown, /api/datasets/<id>
// or /api/runs/<run id>, each answering /preview and /download.

const LARGE_DOWNLOAD = 2 * 1024 ** 3;
const dsPreview = { base: null, name: "", folder: null };

function confirmLargeDownload(size, name) {
  if (!size || size < LARGE_DOWNLOAD) return true;
  return confirm(`"${name}" is ${fmtSize(size)}. Download it anyway?\n\nLarge downloads take a while; keep this tab open until it finishes.`);
}

function dsDownloadUrl(base, file, inline) {
  const q = new URLSearchParams();
  if (file) q.set("file", file);
  if (inline) q.set("inline", "1");
  const qs = q.toString();
  return `${base}/download${qs ? "?" + qs : ""}`;
}

async function openDatasetPreview(base, name, file) {
  const modal = $("datasetPreviewModal"), body = $("dsPreviewBody");
  if (dsPreview.base !== base) dsPreview.folder = null;
  Object.assign(dsPreview, { base, name });
  $("dsPreviewTitle").textContent = file ? file.split("/").pop() : name;
  $("dsPreviewMeta").textContent = "";
  const dl = $("dsPreviewDownload");
  dl.href = dsDownloadUrl(base, file);
  dl.onclick = null;
  body.innerHTML = `<div class="ds-loading">${icon("hourglass_top")} Reading ${escapeHtml(file || name)}…</div>`;
  modal.classList.remove("hidden");
  let data;
  try {
    const q = file ? `?file=${encodeURIComponent(file)}` : "";
    const res = await fetch(`${base}/preview${q}`);
    data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  } catch (err) {
    body.innerHTML = `<div class="ds-error">${icon("error")} ${escapeHtml(isUnreachable(err) ? "Can't reach the server." : err.message)}</div>`;
    return;
  }
  if (dsPreview.base !== base) return;                // another preview was opened meanwhile
  const ds = data.dataset || {};
  const where = ds.location === "hpc3" ? "kept on HPC3" : "kept on the server";
  if (data.folder) {
    dsPreview.folder = data.folder;
    $("dsPreviewMeta").textContent = `Folder · ${data.folder.total_files} files · ${fmtSize(data.folder.total_bytes)} · ${where}`;
    dl.onclick = (e) => { if (!confirmLargeDownload(data.folder.total_bytes, name)) e.preventDefault(); };
    dl.textContent = "Download (.zip)";
    body.innerHTML = dsFolderHtml(base, data.folder) + dsDescriptionSlot(base);
    loadDatasetDescription(base);
    return;
  }
  const f = data.file || {};
  dl.textContent = "Download";
  dl.onclick = (e) => { if (!confirmLargeDownload(f.size_bytes, f.name)) e.preventDefault(); };
  $("dsPreviewMeta").textContent = [dsKindLabel(f.kind, data.preview), fmtSize(f.size_bytes), where].filter(Boolean).join(" · ");
  const back = file && ds.is_folder
    ? `<button type="button" class="ds-back ghost small" data-ds-back="1">${icon("arrow_back")} ${escapeHtml(name)}/</button>` : "";
  body.innerHTML = back + dsPreviewHtml(base, file, data.preview || {}) + (file ? "" : dsDescriptionSlot(base));
  if (!file) loadDatasetDescription(base);
}

// ---- the dataset's description, DATASET.md (issue 10) ----
// Shown below the preview of a dataset (not of one file inside a folder). The PI writes it the first
// time a run reads the dataset; the owner can edit it, and an edit is kept: a later run reads it and
// never writes over it. Every statement is marked as read from the data, inferred, or the owner's.

function dsDescriptionSlot(base) {
  return /^\/api\/datasets\/\d+$/.test(base) ? `<section id="dsDescription" class="ds-description" aria-live="polite"></section>` : "";
}

async function loadDatasetDescription(base) {
  const box = $("dsDescription");
  if (!box) return;
  box.innerHTML = `<div class="ds-note">${icon("hourglass_top")} Reading the description…</div>`;
  let d;
  try {
    const res = await fetch(`${base}/description`);
    d = await res.json();
    if (!res.ok) throw new Error(d.detail || d.error || `HTTP ${res.status}`);
  } catch (err) {
    if (dsPreview.base === base) box.innerHTML = "";     // no accounts, or no record: show nothing
    return;
  }
  if (dsPreview.base === base) renderDatasetDescription(box, base, d);
}

function dsDescriptionWho(d) {
  const day = (iso) => (iso ? String(iso).slice(0, 10) : "");
  if (d.state === "owner") return `Edited by you${d.owner_edited_at ? ` on ${day(d.owner_edited_at)}` : ""}`;
  if (d.state === "pi") return `Written by the PI${d.written_at ? ` on ${day(d.written_at)}` : ""}${d.model ? ` (${d.model})` : ""}`;
  return "";
}

function renderDatasetDescription(box, base, d, editing) {
  const head = `<div class="ds-desc-head"><h3>${icon("description")}<span>Description</span><code>DATASET.md</code></h3>`
    + (d.state !== "none" ? `<span class="ds-desc-who">${escapeHtml(dsDescriptionWho(d))}</span>` : "") + `</div>`;
  if (d.state === "none" && !editing) {
    box.innerHTML = head + `<p class="ds-note">No description yet. The PI writes one the first time a run reads this dataset: `
      + `what the data is, its design, how it was processed, and what it can and cannot support, each statement marked as read from the data or inferred.</p>`;
    return;
  }
  if (editing) {
    box.innerHTML = head
      + `<textarea class="ds-desc-edit" spellcheck="true" aria-label="DATASET.md">${escapeHtml(d.markdown || "")}</textarea>`
      + `<div class="ds-desc-acts"><button type="button" class="primary small" data-desc-save>Save</button>`
      + `<button type="button" class="ghost small" data-desc-cancel>Cancel</button>`
      + `<span class="ds-note">Your text is kept: a later run reads it and does not write over it.</span></div>`;
    const ta = box.querySelector("textarea");
    ta.focus();
    box.querySelector("[data-desc-cancel]").onclick = () => renderDatasetDescription(box, base, d);
    box.querySelector("[data-desc-save]").onclick = async () => {
      const btn = box.querySelector("[data-desc-save]");
      btn.disabled = true;
      try {
        const res = await fetch(`${base}/description`, { method: "PUT", headers: { "Content-Type": "application/json" },
                                                          body: JSON.stringify({ markdown: ta.value }) });
        const out = await res.json();
        if (!res.ok) throw new Error(out.detail || `HTTP ${res.status}`);
        toast("Description saved.");
        renderDatasetDescription(box, base, out);
      } catch (err) { btn.disabled = false; toast(`Not saved: ${err.message}`); }
    };
    return;
  }
  const notes = [];
  if (d.stale) notes.push("The data changed after this was written: the next run that reads the dataset writes it again.");
  if (d.data_changed_after_edit) notes.push("The data changed after you edited this description. Check it still holds.");
  box.innerHTML = head
    + notes.map((n) => `<p class="ds-desc-warn">${icon("info")}<span>${escapeHtml(n)}</span></p>`).join("")
    + `<div class="ds-desc-body">${renderMarkdown(d.markdown || "")}</div>`
    + `<div class="ds-desc-acts"><button type="button" class="ghost small" data-desc-edit>${icon("edit")}<span>Edit</span></button>`
    + `<a class="ghost small" href="${base}/DATASET.md" download>${icon("download")}<span>DATASET.md</span></a>`
    + (d.state === "owner" && d.pi_markdown ? `<button type="button" class="ghost small" data-desc-reset title="Drop your edit and show the PI's description">Restore the PI's version</button>` : "")
    + `</div>`;
  box.querySelector("[data-desc-edit]").onclick = () => renderDatasetDescription(box, base, d, true);
  const reset = box.querySelector("[data-desc-reset]");
  if (reset) reset.onclick = async () => {
    if (!confirm("Drop your edit and show the PI's description again?")) return;
    try {
      const res = await fetch(`${base}/description/edit`, { method: "DELETE" });
      const out = await res.json();
      if (!res.ok) throw new Error(out.detail || `HTTP ${res.status}`);
      renderDatasetDescription(box, base, out);
    } catch (err) { toast(`Not restored: ${err.message}`); }
  };
}

function dsKindLabel(kind, p) {
  if (p && p.compressed && p.type !== "binary") return ({ table: "Table", vcf: "VCF", text: "Text" }[p.type] || "File") + " (gzip)";
  return { h5ad: "AnnData (.h5ad)", hdf5: "HDF5", vcf: "VCF", table: "Table", json: "JSON",
           image: "Image", pdf: "PDF", text: "Text", binary: "Binary file" }[kind] || "File";
}

function dsTable(columns, rows, opts = {}) {
  if (!columns || !columns.length) return "";
  const head = "<tr>" + (opts.index ? `<th class="ds-idx"></th>` : "") +
    columns.map((c, k) => `<th>${escapeHtml(c)}${opts.types && opts.types[k] ? `<span class="ds-type">${escapeHtml(opts.types[k])}</span>` : ""}</th>`).join("") + "</tr>";
  const body = (rows || []).map((r, i) => "<tr>" + (opts.index ? `<th class="ds-idx">${escapeHtml(opts.index[i] ?? "")}</th>` : "") +
    columns.map((_c, k) => `<td>${escapeHtml(r[k] ?? "")}</td>`).join("") + "</tr>").join("");
  return `<div class="ds-table-wrap"><table class="ds-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}

function dsSection(title, inner, note) {
  if (!inner) return "";
  return `<section class="ds-section"><h3>${escapeHtml(title)}</h3>${inner}${note ? `<p class="ds-note">${escapeHtml(note)}</p>` : ""}</section>`;
}

function dsFacts(items) {
  const xs = items.filter((x) => x && x[1] !== undefined && x[1] !== null && x[1] !== "");
  return xs.length ? `<dl class="ds-facts">${xs.map(([k, v]) => `<div><dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd></div>`).join("")}</dl>` : "";
}

function dsNames(list) {
  return (list || []).length
    ? `<ul class="ds-names">${list.map((x) => `<li><code>${escapeHtml(x.name ?? x)}</code>${x.shape ? ` <span class="ds-dim">${escapeHtml(x.shape.join(" × "))}</span>` : ""}</li>`).join("")}</ul>` : "";
}

function dsFolderHtml(base, folder) {
  const rows = (folder.entries || []).map((e) => {
    const p = escapeHtml(e.path);
    return `<tr><td class="ds-path"><button type="button" class="linklike" data-ds-file="${p}">${p}</button></td>` +
      `<td class="ds-size">${fmtSize(e.size)}</td>` +
      `<td class="ds-act"><a class="ghost small" href="${dsDownloadUrl(base, e.path)}" download>Download</a></td></tr>`;
  }).join("");
  const more = folder.truncated ? `<p class="ds-note">Showing the first ${folder.entries.length} of ${folder.total_files} files. The .zip has all of them.</p>` : "";
  return rows
    ? `<div class="ds-table-wrap"><table class="ds-table ds-files"><thead><tr><th>File</th><th>Size</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>${more}`
    : `<div class="ds-note">This folder has no files.</div>`;
}

function dsXValues(x) {
  const s = x && x.sample;
  if (!s) return "";
  const range = `${+s.min.toPrecision(4)} to ${+s.max.toPrecision(4)}`;
  if (s.all_integer && !s.any_negative) return `whole numbers, ${range}, in the first ${s.n} stored values: looks like raw counts`;
  return `${range} in the first ${s.n} stored values, not all whole numbers: already normalized or transformed`;
}

function dsPreviewHtml(base, file, p) {
  switch (p.type) {
    case "h5ad": {
      if (p.note && !p.obs) return `<div class="ds-note">${escapeHtml(p.note)}</div>`;
      const x = p.X || {};
      const facts = dsFacts([
        ["Cells (obs)", (p.n_obs ?? "").toLocaleString()], ["Genes (var)", (p.n_vars ?? "").toLocaleString()],
        ["Matrix X", x.present ? [x.encoding, x.dtype].filter(Boolean).join(", ") : "none"],
        ["Values in X", dsXValues(x)],
        [".raw", p.raw && p.raw.present ? `yes${p.raw.n_vars ? `, ${p.raw.n_vars.toLocaleString()} genes` : ""}` : "no"],
      ]);
      const cols = (frame) => frame && frame.columns && frame.columns.length ? dsTable(["Column", "Type", "Values"],
        frame.columns.map((c) => [c.name, c.dtype || "", c.categories ? `${c.n_categories} ${c.n_categories === 1 ? "category" : "categories"}: ${c.categories.join(", ")}${c.n_categories > c.categories.length ? ", …" : ""}` : (c.note || "")])) : "";
      const head = (frame) => frame && frame.head && frame.head.columns.length ? dsTable(frame.head.columns, frame.head.rows, { index: frame.head.index }) : "";
      return facts +
        dsSection("Cell metadata (obs)", cols(p.obs), p.obs && p.obs.n_columns > (p.obs.columns || []).length ? `${p.obs.n_columns} columns; the first ${p.obs.columns.length} are listed.` : "") +
        dsSection("First cells", head(p.obs)) +
        dsSection("Gene metadata (var)", cols(p.var)) +
        dsSection("First genes", head(p.var)) +
        dsSection("Layers", dsNames(p.layers)) +
        dsSection("Embeddings (obsm)", dsNames(p.obsm)) +
        dsSection("Graphs (obsp)", dsNames(p.obsp)) +
        dsSection("Unstructured (uns)", dsNames(p.uns)) +
        (p.note ? `<p class="ds-note">${escapeHtml(p.note)}</p>` : "");
    }
    case "hdf5": {
      const cr = p.cellranger;
      const facts = cr ? dsFacts([["Features", cr.features.toLocaleString()], ["Barcodes", cr.barcodes.toLocaleString()],
        ["Feature types", Object.entries(cr.feature_types || {}).map(([k, v]) => `${k} (${v.toLocaleString()})`).join(", ")]]) : "";
      const nodes = (p.nodes || []).map((n) => [n.path, n.kind, n.shape ? n.shape.join(" × ") : "", n.dtype || ""]);
      return facts + dsSection("Structure", dsTable(["Path", "Kind", "Shape", "Type"], nodes), p.more_nodes ? "The first nodes are listed." : "") +
        (p.note ? `<p class="ds-note">${escapeHtml(p.note)}</p>` : "");
    }
    case "table":
      return dsTable(p.columns, p.rows, { types: p.column_types }) +
        `<p class="ds-note">${escapeHtml(p.note || "")} ${p.more_rows ? `Showing the first ${p.rows_shown} rows.` : `${p.rows_shown} ${p.rows_shown === 1 ? "row" : "rows"}.`}</p>`;
    case "vcf":
      return dsFacts([["Format", p.fileformat], ["Reference", p.reference], ["Contigs", p.n_contigs],
                      ["Samples", p.n_samples ? `${p.n_samples}: ${p.samples.slice(0, 12).join(", ")}${p.n_samples > 12 ? ", …" : ""}` : "none (sites only)"],
                      ["INFO fields", (p.info_fields || []).join(", ")], ["FORMAT fields", (p.format_fields || []).join(", ")]]) +
        dsSection("First records", dsTable(p.columns, p.rows), p.more_rows ? `Showing the first ${p.rows.length} records.` : "") +
        (p.header_complete ? "" : `<p class="ds-note">The header is longer than the part read for the preview.</p>`);
    case "image":
      return `<div class="ds-media"><img src="${dsDownloadUrl(base, file, true)}" alt="${escapeHtml(file || dsPreview.name)}" /></div>`;
    case "pdf":
      return `<iframe class="ds-pdf" src="${dsDownloadUrl(base, file, true)}" title="${escapeHtml(file || dsPreview.name)}"></iframe>`;
    case "text":
      return `<pre class="ds-text">${escapeHtml(p.text || "")}</pre>` + (p.more_rows ? `<p class="ds-note">Showing the beginning of the file.</p>` : "");
    default:
      return `<div class="ds-note">${escapeHtml(p.note || "This file has no preview. Download it to open it.")}</div>`;
  }
}

function onDatasetPreviewClick(e) {
  const f = e.target.closest("[data-ds-file]");
  if (f) { openDatasetPreview(dsPreview.base, dsPreview.name, f.dataset.dsFile); return; }
  if (e.target.closest("[data-ds-back]")) openDatasetPreview(dsPreview.base, dsPreview.name);
}

function closeDatasetPreview() {
  $("datasetPreviewModal").classList.add("hidden");
  $("dsPreviewBody").innerHTML = "";                   // stop a PDF or image that is still loading
  dsPreview.base = null;
}

// ---- Runs view ----
// Researchers just want the results of a past run, not a file browser: each row offers a
// single "Download results (.zip)" of that run's bundle (/api/bundle/<owner>/<run_id>).
async function loadRuns() {
  const el = $("runsList"); el.innerHTML = '<div class="empty-hint">Loading…</div>';
  const owner = state.user ? state.user.username : "";
  try {
    const items = (await (await fetch("/api/runs")).json()).runs || [];
    const results = await loadResultHandoffs(true);
    el.innerHTML = items.length ? items.map((x) => {
      const done = runHasOutputs(x);
      const zip = done
        ? `<button type="button" class="ghost small" data-run-files="${escapeHtml(x.run_id)}" data-name="${escapeHtml(runTitle(x))}" title="Preview the report, figures and tables this run wrote">Files</button>` +
          `<a class="ghost small" href="/api/results/${encodeURIComponent(x.run_id)}/croissant.json" target="_blank" rel="noopener" title="The result's Croissant metadata: its files, inputs and provenance">croissant.json</a>` +
          `<a class="ghost small" href="/api/bundle/${encodeURIComponent(owner)}/${encodeURIComponent(x.run_id)}" download>${icon("download")} Results (.zip)</a>`
        : `<span class="muted small">${escapeHtml(x.status)}</span>`;
      return `<div class="card run-card" data-q="${escapeHtml(runSearchText(x))}"><div class="card-main"><strong>${escapeHtml(runTitle(x))}</strong>` +
        `<span class="muted">${escapeHtml(x.status)}${x.plan_mode ? " · planned" : ""} · ${fmtDate(x.created_at)}</span>${resultHandoffHtml(x.run_id, results[x.run_id])}</div>` +
        `<div class="run-card-actions"><button type="button" class="ghost small" data-run-usage="${escapeHtml(x.run_id)}" title="Tokens and estimated cost of this run, by pipeline step and model">Usage</button>${zip}` +
        `<button type="button" class="ghost small danger" data-run-del="${escapeHtml(x.run_id)}" data-name="${escapeHtml(runTitle(x))}" data-kept="${keptBesideRun(results[x.run_id]) ? "1" : ""}" title="Delete this run and its files from the server">Delete</button></div></div>`;
    }).join("") : '<div class="empty-hint">No runs yet.</div>';
  } catch { el.innerHTML = '<div class="empty-hint">Failed to load.</div>'; }
  filterCards("runsList", "runsCount", $("runsSearch").value);
}

// What a run handed on that is still kept beside it on this host (not saved to the user's data, not on
// HPC3): deleting the run deletes it too.
function keptBesideRun(r) {
  const here = (x) => x && x.name && !x.saved_to && !x.saving && x.location !== "hpc3";
  return !!r && (here(r) || here(r.cellqc) || here(r.literature));
}
function runHasOutputs(x) { return x.status === "done" || x.status === "incomplete"; }
function runTitle(x) { return (x.question || "").slice(0, 90) || `Run ${x.run_id}`; }
function runSearchText(x) {
  const from = chatLabel(x.conversation_id);
  return sysSearchText([x.question, x.status, x.plan_mode ? "planned" : "", x.run_id, from && from.text,
                        fmtDate(x.created_at), (x.created_at || "").slice(0, 10)]);
}

// Files → Outputs: the runs that wrote something, each opening its artifacts in the dataset viewer.
async function loadOutputs() {
  const el = $("outputsList"); el.innerHTML = '<div class="empty-hint">Loading…</div>';
  const owner = state.user ? state.user.username : "";
  try {
    const items = ((await (await fetch("/api/runs")).json()).runs || []).filter(runHasOutputs);
    const results = await loadResultHandoffs(true);
    el.innerHTML = items.length ? items.map((x) =>
      `<div class="card run-card" data-q="${escapeHtml(runSearchText(x))}"><div class="card-main"><strong>${icon("description")} ${escapeHtml(runTitle(x))}</strong>` +
      `<span class="muted">${escapeHtml(x.status)} · ${fmtDate(x.created_at)} · run ${escapeHtml(x.run_id)}</span>${resultHandoffHtml(x.run_id, results[x.run_id])}</div>` +
      `<div class="run-card-actions"><button type="button" class="ghost small" data-run-files="${escapeHtml(x.run_id)}" data-name="${escapeHtml(runTitle(x))}" title="Preview the report, figures and tables this run wrote">Preview</button>` +
      `<a class="ghost small" href="/api/results/${encodeURIComponent(x.run_id)}/croissant.json" target="_blank" rel="noopener" title="The result's Croissant metadata: its files, inputs and provenance">croissant.json</a>` +
      `<a class="ghost small" href="/api/bundle/${encodeURIComponent(owner)}/${encodeURIComponent(x.run_id)}" download>${icon("download")} Results (.zip)</a></div></div>`).join("")
      : '<div class="empty-hint">No outputs yet: a finished run\'s report, figures and tables appear here.</div>';
  } catch { el.innerHTML = '<div class="empty-hint">Failed to load.</div>'; }
  filterCards("outputsList", "outputsCount", $("outputsSearch").value);
}
function onOutputsClick(e) {
  const f = e.target.closest("[data-run-files]");
  if (f) { openDatasetPreview(`/api/runs/${encodeURIComponent(f.dataset.runFiles)}`, f.dataset.name || ""); return; }
  onSaveResultClick(e);
}

async function deleteRun(b) {
  const what = b.dataset.kept ? ", and the processed data kept beside it (not saved to your data)" : "";
  if (!confirm(`Delete the run "${b.dataset.name}"?\n\nThis removes from the server its report, figures, tables and run record${what}. ` +
               `Datasets you saved from it stay in Files → Datasets, and what it cost stays in your usage. ` +
               `Its working files on HPC3 go when your session ends. This cannot be undone.`)) return;
  b.disabled = true;
  try {
    const res = await fetch("/api/runs/delete", { method: "POST", headers: { "Content-Type": "application/json" },
                                                  body: JSON.stringify({ run_id: b.dataset.runDel }) });
    const j = await res.json();
    if (!res.ok) { toast(j.error || "Delete failed"); b.disabled = false; return; }
    toast("Run deleted.");
    resultHandoffs = null;
    return true;
  } catch (err) {
    toast(isUnreachable(err) ? "Can't reach the server." : "Delete failed: " + err.message);
    b.disabled = false;
  }
  return false;
}

// What a run hands on besides its bundle, neither of them in the zip: the processed dataset (an
// .h5ad) and, after CellQC, CellQC's whole result folder with the per-library matrices. Both are
// kept beside the run on HPC3 until the session ends, then copied to the run's folder on the server; "Save to my data" makes one a dataset, and the folder also
// downloads as one zip. Shown on the run's card and under the results in its chat.
let resultHandoffs = null;              // a promise of { run id: row }; null = ask the server again
function loadResultHandoffs(fresh) {
  if (fresh || !resultHandoffs) {
    resultHandoffs = fetch("/api/result-datasets").then((r) => r.json())
      .then((d) => Object.fromEntries((d.result_datasets || []).map((r) => [r.run_id, r]))).catch(() => ({}));
  }
  return resultHandoffs;
}
function resultHandoffHtml(runId, r) {
  if (!r) return "";
  const mb = (n) => `${(Number(n || 0) / 1e6).toFixed(0)} MB`;
  const kept = (x, label, what) => (x.saved_to
    ? `<span class="ok small" title="${escapeHtml(`In Files → Datasets as ${x.name || label}`)}">Saved to my data</span>`
    : x.saving ? `<span class="muted small">Copying from HPC3 to your data on the server…</span>`
    : `<span class="muted small">${x.until_disconnect ? "kept on HPC3 until the session ends, then with the run on the server"
      : x.location !== "hpc3" && !x.kept_days ? "kept with the run on the server"
      : `kept ${x.location === "hpc3" ? "on HPC3" : "on the server"} for ${escapeHtml(String(x.kept_days ?? ""))} days`}</span>` +
      (x.save_error ? `<span class="bad small" title="${escapeHtml(x.save_error)}">The last save failed</span>` : "") +
      `<button class="ghost small" data-save-result="${escapeHtml(runId)}" data-what="${escapeHtml(what)}" data-label="${escapeHtml(label)}">${escapeHtml(label)}</button>`);
  let html = "";
  const datasets = Array.isArray(r.datasets) ? r.datasets : (r.name ? [r] : []);
  for (const dataset of datasets) {
    html += `<div class="run-dataset"><span class="muted small">📦 ${escapeHtml(dataset.name)} · ${dataset.n_cells} cells × ${dataset.n_genes} genes · ${mb(dataset.size_bytes)}</span>` +
      kept(dataset, "Save to my data", dataset.what || "dataset") + `</div>`;
  }
  const q = r.cellqc;
  if (q) {
    const matrices = q.n_matrices ? `, ${q.n_matrices} per-library .h5ad` : "";
    html += `<div class="run-dataset"><span class="muted small" title="${escapeHtml(q.name || "")}">🧪 CellQC result/ · ${q.n_files} files${matrices} · ${mb(q.size_bytes)}</span>` +
      `<a class="ghost small" href="${escapeHtml(q.download_url)}" download title="CellQC's complete result folder as one zip: reports, tables, slides and the per-library .h5ad, obs and var">${icon("download")} CellQC results (.zip)</a>` +
      kept(q, "Save folder to my data", "cellqc") + `</div>`;
  }
  // A corpus curation's two tables (issue 46): kept with the run's results; saving copies them into a dataset.
  const t = r.tables;
  if (t) {
    const files = (t.files || []).join(", ");
    html += `<div class="run-dataset"><span class="muted small" title="${escapeHtml(files)}">🧬 RetiGene curation tables · ${escapeHtml(String(t.n_genes ?? 0))} haploinsufficient genes · ${escapeHtml(String((t.files || []).length))} CSV files</span>` +
      (t.saved_to
        ? `<span class="ok small" title="${escapeHtml(`In Files → Datasets as ${t.name || "the curation tables"}`)}">Saved to my data</span>`
        : (t.save_error ? `<span class="bad small" title="${escapeHtml(t.save_error)}">The last save failed</span>` : "") +
          `<button class="ghost small" data-save-result="${escapeHtml(runId)}" data-what="tables" data-label="Save tables to my data">Save tables to my data</button>`) +
      `</div>`;
  }
  const literature = r.literature;
  if (literature) {
    html += `<div class="run-dataset"><span class="muted small">Literature tables · ${literature.n_files} files · ${mb(literature.size_bytes)}</span>` +
      kept(literature, "Save tables to my data", "literature") + `</div>`;
  }
  return html;
}
async function showResultHandoff(host, runId) {
  if (!host) return;
  host.dataset.run = runId;
  const r = (await loadResultHandoffs())[runId];
  host.innerHTML = resultHandoffHtml(runId, r);
}
// A result kept on HPC3 is copied back to the server in the background; the server says when it is here.
async function onResultSaved(msg) {
  await loadResultHandoffs(true);
  document.querySelectorAll(".result-handoff").forEach((host) => {
    if (host.dataset.run === String(msg.run_id)) showResultHandoff(host, host.dataset.run);
  });
  if ($("view-runs") && $("view-runs").classList.contains("active")) loadRuns();
  if (outputsShown()) loadOutputs();
  onDatasetsChanged();
}
function outputsShown() {
  return filesTab === "outputs" && $("view-datasets") && $("view-datasets").classList.contains("active");
}
function onDatasetsChanged() {
  if ($("view-datasets") && $("view-datasets").classList.contains("active")) loadDatasets();
  loadDatasetChips();   // a moved dataset's chips, and the chats it is attached to, take its new path
}

async function onSaveResultClick(e) {
  const b = e.target.closest("[data-save-result]");
  if (!b) return;
  const runId = b.dataset.saveResult, label = b.dataset.label || "Save to my data";
  b.disabled = true; b.textContent = "Saving…";
  try {
    const r = await fetch(`/api/result-datasets/${encodeURIComponent(runId)}/save?what=${encodeURIComponent(b.dataset.what || "dataset")}`, { method: "POST" });
    const j = await r.json();
    if (!r.ok) { toast(j.error || "Could not save it."); b.disabled = false; b.textContent = label; return; }
    if (j.saving) {
      b.textContent = "Copying to the server…";
      toast("Copying it from HPC3 to your data on the server. It appears in Files → Datasets when it is there.");
      await loadResultHandoffs(true);
      return;
    }
    // Named as Files → Datasets lists it: the reply's path is the folder on the server (issue 37).
    toast(`Saved ${j.name ? `“${j.name}”` : "it"} to your data: it is in Files → Datasets now.`);
    await loadResultHandoffs(true);
    const inChat = b.closest(".result-handoff");
    if (inChat) showResultHandoff(inChat, runId); else if (outputsShown()) loadOutputs(); else loadRuns();
    loadDatasets();
  } catch (err) { toast("Could not save it: " + err.message); b.disabled = false; b.textContent = label; }
}
function onRunsClick(e) {
  const u = e.target.closest("[data-run-usage]");
  if (u) { toggleRunUsage(u); return; }
  const d = e.target.closest("[data-run-del]");
  if (d) { deleteRun(d).then((gone) => { if (gone) loadRuns(); }); return; }
  onOutputsClick(e);
}

// ---- Admin view ----
async function loadAdminUsers() {
  const body = $("userTableBody"); body.innerHTML = '<tr><td colspan="7" class="empty-hint">Loading…</td></tr>';
  const q = ($("userSearch") && $("userSearch").value.trim()) || "";
  try {
    const url = q ? `/api/admin/users?q=${encodeURIComponent(q)}` : "/api/admin/users";
    const r = await fetch(url);
    if (!r.ok) { body.innerHTML = '<tr><td colspan="7" class="empty-hint">Admin only.</td></tr>'; return; }
    const users = (await r.json()).users || [];
    if (!users.length) { body.innerHTML = `<tr><td colspan="7" class="empty-hint">${q ? "No users match your search." : "No users."}</td></tr>`; return; }
    const meId = state.user && state.user.id;
    body.innerHTML = users.map((u) =>
      `<tr><td>${u.id}</td><td>${escapeHtml(u.username)}</td>` +
      `<td class="muted">${u.email ? escapeHtml(u.email) : "—"}</td><td>${u.role}</td>` +
      `<td>${u.is_active ? '<span class="ok">active</span>' : '<span class="bad">disabled</span>'}</td>` +
      `<td class="muted">${fmtDate(u.last_login_at)}</td><td class="row-actions">` +
      (u.is_active ? `<button class="ghost small" data-disable="${u.id}">Disable</button>`
                   : `<button class="ghost small" data-enable="${u.id}">Enable</button>`) +
      `<button class="ghost small" data-email="${u.id}" data-cur="${escapeHtml(u.email || "")}" data-name="${escapeHtml(u.username)}">Email</button>` +
      `<button class="ghost small" data-reset="${u.id}" data-name="${escapeHtml(u.username)}">Reset pw</button>` +
      // Role toggle: hidden for your own row (self-change is blocked server-side).
      (u.id === meId ? "" : `<button class="ghost small" data-role="${u.id}" data-to="${u.role === "admin" ? "user" : "admin"}" data-name="${escapeHtml(u.username)}">${u.role === "admin" ? "Make user" : "Make admin"}</button>`) +
      (u.id === meId ? "" : `<button class="ghost small danger" data-deluser="${u.id}" data-name="${escapeHtml(u.username)}">Delete</button>`) +
      `</td></tr>`).join("");
  } catch { body.innerHTML = '<tr><td colspan="7" class="empty-hint">Failed to load.</td></tr>'; }
}
// ---- Admin: skill review ----
// Induced skills are one run's code frozen into a template; none reaches the PI or the Scientist
// until an admin approves it here (agents/skills.py, "review gate").
const skillReview = { rows: [], filter: "pending", open: {} };

function switchAdminTab(tab) {
  document.querySelectorAll(".admin-tab").forEach((b) => b.classList.toggle("active", b.dataset.adminTab === tab));
  document.querySelectorAll(".admin-pane").forEach((p) => p.classList.toggle("hidden", p.id !== "adminPane-" + tab));
  if (tab === "skills") loadSkillReviews();
  else if (tab === "requests") loadSkillRequests();
  else if (tab === "usage") loadAdminUsage();
}

async function loadSkillReviews() {
  const el = $("skillReviewList");
  try {
    const r = await fetch("/api/admin/skills");
    if (!r.ok) { el.innerHTML = '<div class="empty-hint">Admin only.</div>'; return; }
    const j = await r.json();
    skillReview.rows = j.skills || [];
    skillReview.configured = j.induced_dir_configured;
    const pending = (j.counts && j.counts.pending) || 0;
    const badge = $("skillPendingBadge");
    badge.textContent = pending ? String(pending) : "";
    badge.classList.toggle("hidden", !pending);
    renderSkillReviews();
  } catch { el.innerHTML = '<div class="empty-hint">Failed to load.</div>'; }
}

function renderSkillReviews() {
  const el = $("skillReviewList");
  const rows = skillReview.rows.filter((x) => skillReview.filter === "all" || x.status === skillReview.filter);
  if (!rows.length) {
    el.innerHTML = `<div class="empty-hint">${skillReview.configured === false
      ? "Skill induction is not configured on this server, so there is nothing to review."
      : `No ${skillReview.filter === "all" ? "" : skillReview.filter + " "}skills.`}</div>`;
    return;
  }
  const actions = (x) => {
    const b = [];
    if (x.status !== "approved") b.push(`<button class="ghost small" data-skill-act="approved" data-skill="${escapeHtml(x.name)}">Approve</button>`);
    if (x.status !== "retired") b.push(`<button class="ghost small danger" data-skill-act="retired" data-skill="${escapeHtml(x.name)}">Retire</button>`);
    if (x.status !== "pending") b.push(`<button class="ghost small" data-skill-act="pending" data-skill="${escapeHtml(x.name)}">Back to pending</button>`);
    return b.join("");
  };
  el.innerHTML = rows.map((x) => {
    const decided = x.reviewed_by ? `${x.status} by ${escapeHtml(x.reviewed_by)} · ${fmtDate(x.reviewed_at)}` : "not reviewed";
    const open = skillReview.open[x.name];
    return `<div class="skill-card" data-card="${escapeHtml(x.name)}">` +
      `<div class="skill-card-head"><div class="card-main"><strong>${escapeHtml(x.name)}</strong>` +
      `<span class="muted">learned ${fmtDate(x.created_at)}${x.origin_run ? " · run " + escapeHtml(x.origin_run) : ""}` +
      `${x.supersedes ? " · newer version of " + escapeHtml(x.supersedes) : ""} · ${decided}</span></div>` +
      `<span class="status-pill ${x.status}">${x.status}</span></div>` +
      `<p class="skill-summary">${escapeHtml(x.summary || "")}</p>` +
      (x.origin_step ? `<p class="skill-origin"><span class="muted">Learned from the step:</span> ${escapeHtml(x.origin_step)}</p>` : "") +
      (x.note ? `<p class="skill-origin"><span class="muted">Review note:</span> ${escapeHtml(x.note)}</p>` : "") +
      `<div class="row-actions"><button class="ghost small" data-skill-view="${escapeHtml(x.name)}">${open ? "Hide code" : "View SKILL.md and code"}</button>${actions(x)}</div>` +
      (open ? `<div class="skill-detail">${open}</div>` : "") +
      `</div>`;
  }).join("");
}

async function onSkillReviewClick(e) {
  const view = e.target.closest("[data-skill-view]");
  if (view) {
    const name = view.dataset.skillView;
    if (skillReview.open[name]) { delete skillReview.open[name]; renderSkillReviews(); return; }
    try {
      const r = await fetch(`/api/admin/skills/${encodeURIComponent(name)}`);
      const j = await r.json();
      if (!r.ok) { toast(j.detail || "Could not load the skill."); return; }
      skillReview.open[name] = `<h4>SKILL.md</h4><pre>${escapeHtml(j.skill_md || "")}</pre>` +
        Object.entries(j.files || {}).map(([f, code]) => `<h4>${escapeHtml(f)}</h4><pre>${escapeHtml(code)}</pre>`).join("");
      renderSkillReviews();
    } catch (err) { toast("Could not load the skill: " + err.message); }
    return;
  }
  const act = e.target.closest("[data-skill-act]");
  if (!act) return;
  const name = act.dataset.skill, status = act.dataset.skillAct;
  const verb = { approved: "Approve", retired: "Retire", pending: "Move back to pending" }[status];
  const note = prompt(`${verb} '${name}'? Optional note for the record:`, "");
  if (note === null) return;
  try {
    const r = await fetch(`/api/admin/skills/${encodeURIComponent(name)}/review`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status, note }) });
    const j = await r.json();
    if (!r.ok) { toast(j.detail || "Review failed."); return; }
    toast(status === "approved" ? `${name} approved: runs can now plan with it.`
      : status === "retired" ? `${name} retired: no run will be offered it.` : `${name} is pending review again.`);
    loadSkillReviews();
  } catch (err) { toast("Review failed: " + err.message); }
}

// --- Skill requests (issue 13): runs that needed code the AI wrote, kept for the team ---
const skillRequests = { rows: [], groups: [], filter: "new", open: {} };
const SKILL_REQUEST_LABELS = { new: "new", planned: "planned", in_progress: "in progress", released: "released", wont_do: "won't do" };

async function loadSkillRequests() {
  const el = $("skillRequestList");
  try {
    const f = skillRequests.filter;
    const r = await fetch("/api/admin/skill-requests" + (f && f !== "all" ? "?status=" + encodeURIComponent(f) : ""));
    if (!r.ok) { el.innerHTML = '<div class="empty-hint">Admin only.</div>'; return; }
    const j = await r.json();
    skillRequests.rows = j.requests || [];
    skillRequests.groups = j.groups || [];
    skillRequests.recording = j.recording;
    skillRequests.statuses = j.statuses || Object.keys(SKILL_REQUEST_LABELS);
    const fresh = (j.counts && j.counts.new) || 0;
    const badge = $("skillRequestBadge");
    badge.textContent = fresh ? String(fresh) : "";
    badge.classList.toggle("hidden", !fresh);
    renderSkillRequests();
  } catch { el.innerHTML = '<div class="empty-hint">Failed to load.</div>'; }
}

/** The server's groups of similar requests (same category and closest skill), most-requested first. */
function skillRequestGroupsHtml(groups) {
  groups = groups || [];
  if (groups.length < 2 && !(groups[0] && groups[0].requests > 1)) return "";
  return `<table class="user-table req-groups"><thead><tr><th>Category</th><th>Closest skill</th><th>Requests</th><th>Accounts</th><th>Latest</th></tr></thead><tbody>` +
    groups.map((g) => `<tr><td>${escapeHtml(g.category)}</td><td>${g.closest_skill ? `<code>${escapeHtml(g.closest_skill)}</code>` : '<span class="muted">none</span>'}</td>` +
      `<td>${g.requests}</td><td>${g.accounts}</td><td>${fmtDate(g.latest)}</td></tr>`).join("") +
    `</tbody></table>`;
}

function renderSkillRequests() {
  const el = $("skillRequestList");
  const rows = skillRequests.rows;
  $("skillRequestGroups").innerHTML = skillRequestGroupsHtml(skillRequests.groups);
  if (!rows.length) {
    el.innerHTML = `<div class="empty-hint">${skillRequests.recording === false
      ? "Recording is off on this server (AISCIENTIST_SKILL_REQUESTS=0)."
      : `No ${skillRequests.filter === "all" ? "" : (SKILL_REQUEST_LABELS[skillRequests.filter] || skillRequests.filter) + " "}requests.`}</div>`;
    return;
  }
  el.innerHTML = rows.map((x) => {
    const o = x.outcome || {};
    const open = skillRequests.open[x.id];
    const moves = (skillRequests.statuses || []).filter((st) => st !== x.status)
      .map((st) => `<button class="ghost small${st === "wont_do" ? " danger" : ""}" data-req-act="${st}" data-req="${escapeHtml(x.id)}">${escapeHtml(SKILL_REQUEST_LABELS[st] || st)}</button>`).join("");
    return `<div class="skill-card" data-card="${escapeHtml(x.id)}">` +
      `<div class="skill-card-head"><div class="card-main"><strong>${escapeHtml(x.one_line || "(no summary yet)")}</strong>` +
      `<span class="muted">${fmtDate(x.created_at)} · ${escapeHtml(x.account || "unknown account")} · run ${escapeHtml(x.run_id || x.id)} · ${escapeHtml(x.category || "other")}` +
      `${x.closest_skill ? " · closest: " + escapeHtml(x.closest_skill) : ""}</span></div>` +
      `<span class="status-pill req-${escapeHtml(x.status)}">${escapeHtml(SKILL_REQUEST_LABELS[x.status] || x.status)}</span></div>` +
      (x.missing ? `<p class="skill-summary"><span class="muted">Missing:</span> ${escapeHtml(x.missing)}</p>` : "") +
      `<p class="skill-origin">${o.untested_accepted || 0} of ${o.accepted_steps || 0} completed steps from untested code · ${o.code_attempts || 0} code attempts, ${o.code_failures || 0} failed` +
      `${(x.pipelines || []).length ? " · pipelines: " + x.pipelines.map(escapeHtml).join(", ") : " · no pipeline matched"}` +
      `${(x.skills || []).length ? " · vetted skill: " + x.skills.map(escapeHtml).join(", ") : ""}` +
      `${(x.induced_skills || []).length ? " · induced skill: " + x.induced_skills.map(escapeHtml).join(", ") : ""}</p>` +
      ((x.untested_steps || []).length ? `<p class="skill-origin"><span class="muted">Untested steps:</span> ${x.untested_steps.map((t) => escapeHtml(planStepParts(t).title)).join(" · ")}</p>` : "") +
      (x.raw_expired ? `<p class="skill-origin muted">Past the retention period: the request text and the code were removed.</p>` : "") +
      `<div class="row-actions"><button class="ghost small" data-req-view="${escapeHtml(x.id)}">${open ? "Hide details" : "View request and code"}</button>${moves}</div>` +
      (open ? `<div class="skill-detail">${open}</div>` : "") +
      `</div>`;
  }).join("");
}

function skillRequestDetailHtml(j) {
  const s = j.summary || {};
  const list = (v) => (v || []).length ? v.map(escapeHtml).join(", ") : '<span class="muted">—</span>';
  let h = `<h4>Request</h4><pre>${escapeHtml(j.request || "(removed)")}</pre>`;
  if ((j.earlier_requests || []).length) h += `<h4>Earlier in the chat</h4><pre>${escapeHtml(j.earlier_requests.join("\n\n"))}</pre>`;
  h += `<h4>Summary${j.summary_by ? " (" + escapeHtml(j.summary_by) + ")" : ""}</h4>` +
    `<p class="skill-origin"><span class="muted">Goal:</span> ${escapeHtml(s.goal || "—")}</p>` +
    `<p class="skill-origin"><span class="muted">Inputs:</span> ${list(s.inputs)} · <span class="muted">Outputs:</span> ${list(s.outputs)} · <span class="muted">Methods:</span> ${list(s.methods)}</p>`;
  if ((j.datasets || []).length) h += `<h4>Data</h4><pre>${escapeHtml(j.datasets.map((d) => Object.entries(d).map(([k, v]) => `${k}: ${v}`).join(" · ")).join("\n"))}</pre>`;
  for (const st of j.steps || []) {
    if (!(st.code_calls || []).length) continue;
    h += `<h4>${escapeHtml(planStepParts(st.step).title)} — ${st.accepted ? "accepted" : "not accepted"}, result from ${escapeHtml(st.status === "untested" ? "untested code" : st.status === "tested" ? "a tested tool" : "no tool")}</h4>`;
    if (st.critique) h += `<p class="skill-origin"><span class="muted">Critic:</span> ${escapeHtml(st.critique)}</p>`;
    st.code_calls.forEach((c, i) => {
      h += `<p class="skill-origin">Attempt ${i + 1} · <code>${escapeHtml(c.tool)}</code>${c.skill ? " · skill " + escapeHtml(c.skill) : ""} · ${c.ok ? "ran" : "failed"}</p>` +
        (c.code ? `<pre>${escapeHtml(c.code)}</pre>` : "") +
        (c.error ? `<pre class="err">${escapeHtml(c.error)}</pre>` : c.summary ? `<p class="skill-origin">${escapeHtml(c.summary)}</p>` : "");
    });
  }
  if ((j.status_history || []).length) h += `<h4>Status history</h4><pre>${escapeHtml(j.status_history.map((x) => `${x.at}  ${x.from} → ${x.to}  by ${x.by}${x.note ? "  — " + x.note : ""}`).join("\n"))}</pre>`;
  return h;
}

async function onSkillRequestClick(e) {
  const view = e.target.closest("[data-req-view]");
  if (view) {
    const id = view.dataset.reqView;
    if (skillRequests.open[id]) { delete skillRequests.open[id]; renderSkillRequests(); return; }
    try {
      const r = await fetch(`/api/admin/skill-requests/${encodeURIComponent(id)}`);
      const j = await r.json();
      if (!r.ok) { toast(j.detail || "Could not load the request."); return; }
      skillRequests.open[id] = skillRequestDetailHtml(j);
      renderSkillRequests();
    } catch (err) { toast("Could not load the request: " + err.message); }
    return;
  }
  const act = e.target.closest("[data-req-act]");
  if (!act) return;
  const id = act.dataset.req, status = act.dataset.reqAct;
  const note = prompt(`Mark this request "${SKILL_REQUEST_LABELS[status] || status}"? Optional note for the record:`, "");
  if (note === null) return;
  try {
    const r = await fetch(`/api/admin/skill-requests/${encodeURIComponent(id)}/status`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status, note }) });
    const j = await r.json();
    if (!r.ok) { toast(j.detail || "Could not change the status."); return; }
    toast(`Request marked ${SKILL_REQUEST_LABELS[status] || status}.`);
    loadSkillRequests();
  } catch (err) { toast("Could not change the status: " + err.message); }
}

async function createUser(e) {
  e.preventDefault();
  const payload = { username: $("cuUser").value.trim(), password: $("cuPass").value, role: $("cuRole").value, email: $("cuEmail").value.trim() || null };
  const r = await fetch("/api/admin/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.detail || "Create failed"); return; }
  toast(`Created ${payload.username}`); $("createUserForm").reset(); loadAdminUsers();
}
async function onUserTableClick(e) {
  const dis = e.target.closest("[data-disable]"), en = e.target.closest("[data-enable]"),
        rs = e.target.closest("[data-reset]"), del = e.target.closest("[data-deluser]"),
        em = e.target.closest("[data-email]"), role = e.target.closest("[data-role]");
  if (role) {
    const to = role.dataset.to;
    if (!confirm(`Change "${role.dataset.name}" to ${to}?`)) return;
    const r = await fetch(`/api/admin/users/${role.dataset.role}/role`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ role: to }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { toast(d.detail || "Role change failed"); return; }
    toast(`${role.dataset.name} is now ${to}`); loadAdminUsers();
    return;
  }
  if (em) {
    const val = prompt(`Set email for ${em.dataset.name} (blank to clear):`, em.dataset.cur || "");
    if (val === null) return;
    const r = await fetch(`/api/admin/users/${em.dataset.email}/email`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: val.trim() }) });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { toast(d.detail || "Update failed"); return; }
    toast("Email updated"); loadAdminUsers();
    return;
  }
  if (dis) { await fetch(`/api/admin/users/${dis.dataset.disable}/active?active=false`, { method: "POST" }); loadAdminUsers(); }
  else if (en) { await fetch(`/api/admin/users/${en.dataset.enable}/active?active=true`, { method: "POST" }); loadAdminUsers(); }
  else if (rs) {
    const pw = prompt(`New password for ${rs.dataset.name} (≥6 chars):`);
    if (!pw) return;
    const r = await fetch(`/api/admin/users/${rs.dataset.reset}/reset-password`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ new_password: pw }) });
    toast(r.ok ? "Password reset" : "Reset failed");
  }
  else if (del) {
    if (!confirm(`Delete user "${del.dataset.name}" and ALL their data (datasets, runs, chats, and files on disk)?\n\nThis cannot be undone.`)) return;
    const r = await fetch(`/api/admin/users/${del.dataset.deluser}`, { method: "DELETE" });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) { toast(d.detail || "Delete failed"); return; }
    toast(`Deleted ${del.dataset.name}`); loadAdminUsers();
  }
}

// ---- Account view: two tabs, API usage (the key, its usage, the budget) and Password ----
function renderAccount(user) {
  const el = $("accountInfo");
  const pwTab = $("accountTab-password");
  if (!user) {
    el.innerHTML = '<p class="muted">Accounts are disabled on this server.</p>';
    $("logoutBtn").classList.add("hidden"); pwTab.classList.add("hidden"); switchAccountTab("usage");
    return;
  }
  $("logoutBtn").classList.remove("hidden"); pwTab.classList.remove("hidden");
  el.innerHTML = accountInfoHtml(user);
}

function accountInfoHtml(user) {
  const since = user.created_at ? fmtDay(String(user.created_at).slice(0, 10), { year: true }) : "";
  const bits = [user.email ? escapeHtml(user.email) : "", since ? `Member since ${escapeHtml(since)}` : ""].filter(Boolean);
  return `<span class="account-avatar" aria-hidden="true">${escapeHtml((user.username || "?").slice(0, 1).toUpperCase())}</span>` +
    `<div class="account-id"><div><strong>${escapeHtml(user.username)}</strong> <span class="role-chip">${escapeHtml(user.role)}</span></div>` +
    (bits.length ? `<div class="muted">${bits.join(" · ")}</div>` : "") + `</div>`;
}

function switchAccountTab(tab, { load = true } = {}) {
  state.accountTab = tab;
  document.querySelectorAll(".account-tab").forEach((b) => {
    const on = b.dataset.accountTab === tab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
    b.tabIndex = on ? 0 : -1;
  });
  document.querySelectorAll(".account-pane").forEach((p) => p.classList.toggle("hidden", p.id !== "accountPane-" + tab));
  if (load && tab === "storage" && storageShown()) loadStorageTab();
}

// The chat counter, the budget notice and the key dialog lead here, to the usage tab; the session
// chip, to the storage tab. switchView reads the tab that is open.
function openAccount(tab = "usage") {
  switchAccountTab(tab, { load: false });
  switchView("account");
}

// The new password is typed twice: a typo here would lock the account until an admin resets it.
function passwordProblem(oldPw, newPw, again) {
  if (!oldPw) return "Enter your current password.";
  if (newPw.length < 6) return "The new password needs at least 6 characters.";
  if (newPw !== again) return "The two new passwords are not the same.";
  return "";
}

async function changePassword(e) {
  e.preventDefault();
  const problem = passwordProblem($("cpOld").value, $("cpNew").value, $("cpNew2").value);
  const box = $("cpProblem");
  box.textContent = problem; box.classList.toggle("hidden", !problem);
  if (problem) return;
  const payload = { old_password: $("cpOld").value, new_password: $("cpNew").value };
  const r = await fetch("/api/account/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) {
    const why = typeof d.detail === "string" && d.detail ? d.detail[0].toUpperCase() + d.detail.slice(1) : "Change failed";
    box.textContent = why; box.classList.remove("hidden"); toast(why); return;
  }
  toast("Password updated"); $("changePwForm").reset();
}

// ---- LLM usage and estimated cost (Account › API usage, the chat counter, run details, budgets) ----
// Every model call is recorded server-side with its token counts; the dollars are ESTIMATES from a
// configurable price table (or the provider's own figure when it reports one). The Claude Console
// stays the authoritative bill, and every view here says so.
const GUIDE_URL = "https://github.com/RCHENLAB/AiScientist/blob/main/docs/claude_api_guide.md";

function userTz() { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { return ""; } }

function fmtUsd(x, { approx = true } = {}) {
  const v = Number(x || 0);
  const s = v === 0 ? "$0" : v < 0.01 ? "<$0.01" : v < 100 ? `$${v.toFixed(2)}` : `$${Math.round(v).toLocaleString()}`;
  return (approx && v >= 0.01 ? "~" : "") + s;
}

// The chat counter's tooltip: what an API key paid for, then the lab GPUs' calls apart and free.
function usageTitle(t) {
  if (!t) return "";
  const api = apiPart(t);
  const free = freeParts(t).map((f) => `${f.name}: ${plural(f.calls, "call")} · ${f.total_tokens.toLocaleString()} tokens · free.`);
  const cluster = free.length ? `${free.join("\n")} ${freeNote(t)}` : "";
  if (!api.calls) return `${cluster}\nClick for Account › API usage.`;
  const unpriced = t.unpriced_calls ? `\n${t.unpriced_calls} call(s) on a model the price table does not list are not in the dollar figure.` : "";
  return `${plural(api.calls, "API call")} · ${api.total_tokens.toLocaleString()} API tokens · ${fmtUsd(t.spend_usd)} estimated.` +
    unpriced + (cluster ? `\n${cluster}` : "") +
    "\nAn estimate from AiScientist's own record; the Claude Console is the authoritative bill. Click for Account › API usage.";
}

// "2026-10-18" -> "Oct 18" (or "Oct 18, 2026"). A calendar day, so it is read and written in UTC.
function fmtDay(iso, { year = false } = {}) {
  if (!iso) return "";
  const d = new Date(String(iso).slice(0, 10) + "T00:00:00Z");
  if (isNaN(d)) return String(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC", ...(year ? { year: "numeric" } : {}) });
}

// The chat header counter: the API tokens and estimated cost of this chat, plus the running cost of
// its run while one is going. Calls on the lab's GPUs (the shared model, a session's own cluster GPU)
// are in no dollar figure: they read "shared model free" beside an API figure, or carry the counter
// alone when the chat had no API call.
//   "12K API tokens · ~$0.18"   "12K API tokens · ~$0.18 · cluster GPU free"   "9.9K tokens · free on the shared model"
function chatUsageText(u, running = false) {
  if (!u || !u.chat || !u.chat.calls) return "";
  const api = apiPart(u.chat), onCluster = (u.chat.cluster && u.chat.cluster.calls) || 0;
  if (!api.calls) return `${fmtTokens((u.chat.cluster && u.chat.cluster.total_tokens) || 0)} tokens · free on the ${freeWhere(u.chat)}`;
  let text = `${fmtTokens(api.total_tokens)} API tokens · ${fmtUsd(u.chat.spend_usd)}`;
  if (onCluster) text += ` · ${freeWhere(u.chat)} free`;
  if (running && u.run && apiPart(u.run).calls) text += ` · this run ${fmtUsd(u.run.spend_usd)}`;
  return text;
}

function renderChatUsage() {
  const el = $("chatUsage");
  if (!el) return;
  const c = activeChat();
  const text = authed() && c ? chatUsageText(c.usage, c.running) : "";
  if (!text) { el.classList.add("hidden"); return; }
  el.textContent = text;
  el.title = usageTitle(c.usage.chat);
  el.classList.remove("hidden");
}

async function refreshChatUsage(c = activeChat()) {
  if (!authed() || !c || !c.sid) return;
  try {
    const r = await fetch(`/api/usage/chat/${encodeURIComponent(c.sid)}`);
    if (!r.ok) return;
    const t = await r.json();
    c.usage = { ...(c.usage || {}), chat: t };
    if (chatVisible(c)) renderChatUsage();
  } catch { /* the counter is best-effort */ }
}

function onUsageUpdate(msg) {
  const c = msg.conversation_id && chats.get(String(msg.conversation_id));
  if (!c) return;
  c.usage = { ...(c.usage || {}), ...(msg.chat ? { chat: msg.chat } : {}), ...(msg.run ? { run: msg.run } : {}) };
  if (chatVisible(c)) renderChatUsage();
}

// A budget threshold reached: a notice above the chat, once per threshold and billing cycle.
async function checkBudgetNotices() {
  if (!authed()) return;
  try {
    const r = await fetch(`/api/usage/summary?tz=${encodeURIComponent(userTz())}`);
    if (!r.ok) return;
    const s = await r.json();
    const fresh = s.new_notices || [];
    if (!fresh.length) return;
    const top = Math.max(...fresh);
    const b = s.budget || {};
    const el = $("budgetNotice");
    const over = s.remaining_usd != null && s.remaining_usd <= 0;
    const left = s.remaining_usd != null && s.remaining_usd > 0 ? ` (${fmtUsd(s.remaining_usd)} left)` : "";
    const when = over ? " The estimate is past the credit: once the Console shows it used up, Claude API requests stop until the cycle resets."
      : s.projected_exhaustion ? ` At the current rate it runs out around ${escapeHtml(s.projected_exhaustion)}.` : "";
    el.innerHTML = `<span class="material-symbols-outlined msym">savings</span>` +
      `<span><strong>${top}% of your monthly API credit used</strong> — an estimated ${fmtUsd(s.spent_usd)} of ` +
      `${fmtUsd(b.monthly_credit_usd, { approx: false })} this cycle${left}.${when} ` +
      `<a href="#" data-usage-open>See usage</a> · <a href="${escapeHtml(s.console_url || "")}" target="_blank" rel="noopener">Claude Console</a></span>` +
      `<button type="button" class="icon-btn" data-dismiss aria-label="Dismiss">✕</button>`;
    el.classList.remove("hidden");
    toast(`${top}% of your monthly API credit used (estimate).`);
    fetch("/api/usage/notices/ack", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ thresholds: fresh, tz: userTz() }) }).catch(() => {});
  } catch { /* best-effort */ }
}

function plural(n, word) { const v = n || 0; return `${v.toLocaleString()} ${word}${v === 1 ? "" : "s"}`; }

// Today as the browser's own calendar day, "2026-10-18" (the server is told the same time zone).
function localIsoDay(d = new Date()) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

function nextDay(iso) {
  if (!iso) return "";
  const d = new Date(iso + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString().slice(0, 10);
}

// "Oct 1 – Oct 31, 2026 · day 18 of 31 · resets Nov 1"
function usageCycleText(cyc, today = localIsoDay()) {
  if (!cyc || !cyc.start) return "";
  const day = Math.round((Date.parse(today + "T00:00:00Z") - Date.parse(cyc.start + "T00:00:00Z")) / 86400000) + 1;
  const of = cyc.days_total ? ` · day ${Math.min(Math.max(day, 1), cyc.days_total)} of ${cyc.days_total}` : "";
  return `${fmtDay(cyc.start)} – ${fmtDay(cyc.end, { year: true })}${of} · resets ${fmtDay(nextDay(cyc.end))}`;
}

function usageTile(label, value, sub, cls = "") {
  return `<div class="usage-tile${cls ? " " + cls : ""}"><div class="usage-tile-label">${label}</div>` +
    `<div class="usage-tile-value">${value}</div><div class="usage-tile-sub">${sub}</div></div>`;
}

// The summary row: what this billing cycle has cost so far, in dollars and tokens, and where that
// stands against the budget. The cycle does not follow the breakdown's period picker.
function usageSummaryHtml(d) {
  const t = d.totals || {};
  const cyc = t.cycle || {}, today = t.today || {}, week = t.last_7_days || {};
  const span = (d.budget && d.budget.cycle) || {};
  // The cycle's spend at the rate so far, as the budget's projection reckons it.
  const pace = cyc.spend_usd > 0 && span.days_elapsed >= 1 && span.days_total
    ? `<div class="usage-tile-note">On pace for ${fmtUsd((cyc.spend_usd / span.days_elapsed) * span.days_total)} by ${escapeHtml(fmtDay(span.end))}</div>` : "";
  const unpriced = cyc.unpriced_calls
    ? `<div class="usage-tile-note" title="Calls on a model the price table does not list are not in the dollar figure.">+ ${plural(cyc.unpriced_calls, "call")} on an unpriced model</div>` : "";
  const spend = usageTile("Estimated API spend", fmtUsd(cyc.spend_usd),
    `Today ${fmtUsd(today.spend_usd)} · last 7 days ${fmtUsd(week.spend_usd)}${pace}${unpriced}`);
  const shared = sharedTokensTile(t, d.shared_model);
  return `<div class="usage-tiles">${spend}${apiTokensTile(cyc, { sharedApart: !!shared })}${shared}${budgetTileHtml(d.budget || {})}</div>`;
}

// The lab's shared model, beside the API figures: its tokens this cycle, today and in the last 7
// days. It is free (HPC3 charges the lab's GPUs by time), so it has tokens and no dollars. Shown once
// the account has used it, or wherever the server keeps one (``kept``).
function sharedTokensTile(t, kept = false) {
  const sh = (x) => (x && x.shared) || {};
  const cyc = sh(t.cycle);
  if (!cyc.calls && !kept) return "";
  const split = tokenSplit(cyc);
  return usageTile("Shared model tokens", fmtTokens(cyc.total_tokens || 0),
    `${plural(cyc.calls || 0, "call")} · today ${fmtTokens(sh(t.today).total_tokens || 0)} · last 7 days ${fmtTokens(sh(t.last_7_days).total_tokens || 0)}` +
    (split ? `<div class="usage-tile-note">${split}</div>` : "") +
    `<div class="usage-tile-note" title="${SHARED_FREE}">Free: the lab's GPUs on HPC3</div>`, "usage-tile-shared");
}

// What an API key paid for: the whole minus ``cluster``, the calls on the cluster GPU, which HPC3
// charges by GPU time and not by token, so they cost nothing here and count against no budget.
function apiPart(x) {
  const c = (x && x.cluster) || {};
  const out = { calls: Math.max(0, (x.calls || 0) - (c.calls || 0)) };
  ["input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "total_tokens"]
    .forEach((k) => { out[k] = Math.max(0, (x[k] || 0) - (c[k] || 0)); });
  return out;
}

const CLUSTER_FREE = "HPC3 charges the cluster GPU by the time it runs, not by token: these calls are in no dollar figure or budget.";
const SHARED_FREE = "HPC3 charges the lab's GPUs by the time they run, not by token: calls on the shared model and on a session's own GPU are in no dollar figure or budget.";

// The tooltip of a free figure: the cluster GPU's wording, or the lab GPUs' once the shared model is in it.
function freeNote(x) { return x && x.shared && x.shared.calls ? SHARED_FREE : CLUSTER_FREE; }

// The calls no API key paid for, by where they ran: the lab's shared model (``shared``, a part of
// ``cluster``) and the rest of ``cluster``, a session's own GPU. Only the parts with calls.
function freeParts(x) {
  const c = (x && x.cluster) || {}, sh = (x && x.shared) || {};
  const parts = [];
  if (sh.calls) parts.push({ name: "Shared model", calls: sh.calls, total_tokens: sh.total_tokens || 0 });
  const own = (c.calls || 0) - (sh.calls || 0);
  if (own > 0) parts.push({ name: "Cluster GPU", calls: own, total_tokens: Math.max(0, (c.total_tokens || 0) - (sh.total_tokens || 0)) });
  return parts;
}

function lowerFirst(name) { return name.charAt(0).toLowerCase() + name.slice(1); }

// "shared model", "cluster GPU", or "lab GPUs" when a figure holds calls on both.
function freeWhere(x) {
  const parts = freeParts(x);
  return parts.length > 1 ? "lab GPUs" : parts.length ? lowerFirst(parts[0].name) : "cluster GPU";
}

// ``sharedApart``: the shared model has a tile of its own beside this one, so only a session GPU's part.
function clusterNote(x, sharedApart = false) {
  return freeParts(x).filter((f) => !sharedApart || f.name !== "Shared model").map((f) => `<div class="usage-tile-note" title="${freeNote(x)}">${f.name}: ${plural(f.calls, "call")} · ${fmtTokens(f.total_tokens)} tokens · free</div>`).join("");
}

function apiTokensTile(t, { sharedApart = false } = {}) {
  const api = apiPart(t);
  const split = tokenSplit(api);
  return usageTile("API tokens", fmtTokens(api.total_tokens),
    plural(api.calls, "API call") + (t.errors ? ` · <span class="bad">${t.errors} failed</span>` : "") +
    (split ? `<div class="usage-tile-note">${split}</div>` : "") + clusterNote(t, sharedApart));
}

// Where the cycle's estimated spend stands against the monthly credit: left / over, a bar with the
// warning thresholds on it, the projection, and the hard limit.
function budgetTileHtml(b) {
  const bud = b.budget;
  const resets = fmtDay(nextDay((b.cycle || {}).end));
  if (!bud || !bud.monthly_credit_usd) {
    return usageTile("Budget", "Not set",
      `<a href="#budgetSection" data-budget-edit>Set your monthly credit</a> to see the spend against it.`, "usage-tile-budget unset");
  }
  const pct = b.percent || 0;
  const over = b.remaining_usd != null && b.remaining_usd <= 0;
  const early = b.lasts_cycle === false;
  const tone = over ? "over" : pct >= 80 || early ? "warn" : "ok";
  const chip = over ? "Credit used up" : early ? "Runs out early" : pct >= 80 ? `${pct.toFixed(0)}% used` : "On track";
  const value = over ? `Over by ${fmtUsd(-b.remaining_usd)}` : `${fmtUsd(b.remaining_usd)} left`;
  const marks = (bud.thresholds || []).filter((x) => x < 100).map((x) => `<i class="usage-mark" style="left:${x}%" title="Warns at ${x}%"></i>`).join("");
  const bar = `<div class="usage-bar tone-${tone}" role="img" aria-label="${pct.toFixed(0)}% of the credit used"><i style="width:${Math.min(100, pct)}%"></i>${marks}</div>` +
    `<div class="usage-bar-legend"><span>${fmtUsd(b.spent_usd)} of ${fmtUsd(bud.monthly_credit_usd, { approx: false })}</span><span>${pct.toFixed(0)}%</span></div>`;
  const proj = over ? "Check the Claude Console for what is actually left."
    : early && b.projected_exhaustion ? `At this rate it runs out around <strong>${escapeHtml(fmtDay(b.projected_exhaustion))}</strong>, before the reset on ${escapeHtml(resets)}.`
      : b.lasts_cycle ? `At this rate it lasts until the reset on ${escapeHtml(resets)}.` : `Resets on ${escapeHtml(resets)}.`;
  const hard = bud.hard_limit_usd != null
    ? `<div class="usage-tile-note">Hard limit ${fmtUsd(bud.hard_limit_usd, { approx: false })} · ${bud.hard_limit_mode === "block" ? "then new runs on your key are blocked" : "then each new run asks first"}${b.hard_limit_reached ? ' · <span class="bad">reached</span>' : ""}</div>` : "";
  const label = `Budget <span class="usage-chip tone-${tone}">${chip}</span><a href="#budgetSection" class="usage-tile-link" data-budget-edit>Edit</a>`;
  return usageTile(label, value, bar + `<div>${proj}</div>` + hard, `usage-tile-budget tone-${tone}`);
}

// Daily bars. Dollars when anything was priced, otherwise tokens (only the cluster GPU was used).
function usageChart(daily, { title = `Last ${(daily || []).length} days` } = {}) {
  if (!daily || !daily.length) return "";
  const spendMax = Math.max(...daily.map((d) => d.spend_usd || 0), 0);
  const tokMax = Math.max(...daily.map((d) => d.total_tokens || 0), 0);
  const useTokens = spendMax === 0 && tokMax > 0;
  const top = useTokens ? tokMax : spendMax;
  const what = useTokens ? "tokens" : "estimated spend";
  const peak = useTokens ? `${fmtTokens(tokMax)} tokens` : fmtUsd(spendMax);
  const head = `<div class="usage-chart-head"><span><strong>${escapeHtml(title)}</strong> <span class="muted">· daily ${what}</span></span>` +
    (top > 0 ? `<span class="muted">peak ${peak}</span>` : "") + "</div>";
  if (top === 0) return `<div class="usage-card-box usage-chart-wrap">${head}<p class="usage-empty">No model calls in these ${daily.length} days.</p></div>`;
  const last = daily.length - 1, today = localIsoDay();
  const bars = daily.map((d) => {
    const v = useTokens ? d.total_tokens || 0 : d.spend_usd || 0;
    const h = v > 0 ? Math.max(3, Math.round((v / top) * 100)) : 0;
    const tip = `${fmtDay(d.date)}: ${fmtUsd(d.spend_usd)} · ${(d.total_tokens || 0).toLocaleString()} tokens · ${plural(d.calls, "call")}`;
    return `<div class="usage-day${d.date === today ? " today" : ""}${v ? "" : " zero"}" title="${escapeHtml(tip)}"><i style="height:${h}%"></i></div>`;
  }).join("");
  const mid = Math.floor(last / 2);
  const end = daily[last].date === today ? "Today" : fmtDay(daily[last].date);
  return `<div class="usage-card-box usage-chart-wrap">${head}` +
    `<div class="usage-chart" role="img" aria-label="Daily ${what} from ${escapeHtml(fmtDay(daily[0].date))} to ${escapeHtml(fmtDay(daily[last].date))}, peak ${peak}">${bars}</div>` +
    `<div class="usage-axis"><span>${escapeHtml(fmtDay(daily[0].date))}</span><span>${escapeHtml(fmtDay(daily[mid].date))}</span><span>${escapeHtml(end)}</span></div></div>`;
}

// "40K input · 30K cache write · 1.1M cache read · 52K output", leaving out the classes at zero.
function tokenSplit(x) {
  return [["input_tokens", "input"], ["cache_write_tokens", "cache write"], ["cache_read_tokens", "cache read"], ["output_tokens", "output"]]
    .filter(([k]) => (x[k] || 0) > 0).map(([k, label]) => `${fmtTokens(x[k])} ${label}`).join(" · ");
}

function sharePct(part) {
  const p = part * 100;
  return p > 0 && p < 1 ? "<1%" : `${Math.round(p)}%`;
}

// One breakdown (by model, run, chat, step, account or day). Each row: its name, its calls and
// tokens by class underneath, its estimated cost and share of the whole on the right, and a thin
// bar for that share. Two columns, so it reads the same on a phone as on a laptop.
function usageList(rows, nameCol, nameOf, { empty = "Nothing in this period.", capped = 0 } = {}) {
  if (!rows || !rows.length) return `<p class="usage-empty">${empty}</p>`;
  const spendSum = rows.reduce((a, x) => a + (x.spend_usd || 0), 0);
  const tokSum = rows.reduce((a, x) => a + (x.total_tokens || 0), 0);
  const byTokens = spendSum === 0;
  const items = rows.map((x) => {
    const part = byTokens ? (tokSum ? (x.total_tokens || 0) / tokSum : 0) : (x.spend_usd || 0) / spendSum;
    const onCluster = (x.cluster && x.cluster.calls) || 0;
    const free = onCluster > 0 && onCluster === x.calls;
    const meta = [plural(x.calls, "call"), `${fmtTokens(x.total_tokens || 0)} tokens`];
    if (onCluster && !free) meta.push(`<span title="${freeNote(x)}">${onCluster} on the ${freeWhere(x)}, free</span>`);
    if (x.errors) meta.push(`<span class="bad">${x.errors} failed</span>`);
    const split = tokenSplit(x);
    const unpriced = x.unpriced_calls ? `<span class="usage-unpriced" title="${plural(x.unpriced_calls, "call")} on a model the price table does not list">+?</span>` : "";
    const cost = free ? `<strong class="usage-free" title="${freeNote(x)}">Free</strong><span>${freeWhere(x)}</span>`
      : `<strong>${fmtUsd(x.spend_usd)}${unpriced}</strong><span>${sharePct(part)}</span>`;
    return `<li class="usage-row${free ? " is-free" : ""}"><div class="usage-row-main"><div class="usage-row-name">${nameOf(x)}</div>` +
      `<div class="usage-row-meta">${meta.join(" · ")}${split ? `<span class="usage-row-split">${split}</span>` : ""}</div></div>` +
      `<div class="usage-row-cost">${cost}</div>` +
      `<div class="usage-row-share" aria-hidden="true"><i style="width:${Math.min(100, part * 100).toFixed(1)}%"></i></div></li>`;
  }).join("");
  const more = capped && rows.length >= capped ? `<p class="usage-foot">The ${capped} largest are shown; Export CSV has every call.</p>` : "";
  return `<div class="usage-list"><div class="usage-list-head"><span>${nameCol}</span><span>Est. cost · share${byTokens ? " of tokens" : ""}</span></div>` +
    `<ul>${items}</ul></div>${more}`;
}

const ERROR_TEXT = {
  credit: "Out of credit — the key's balance is used up for this cycle",
  rate: "Rate-limited — too many requests or tokens per minute for the key's tier",
  auth: "Authentication failed — the key was revoked or mistyped",
  model: "Model not available to this key",
  context: "Prompt longer than the model's context window",
  network: "Could not reach the provider",
  endpoint: "The provider returned an error",
  refusal: "The model declined the request",
  unavailable: "The shared model was not answering (its GPU job was being renewed)",
  busy: "The shared model was busy: the request waited too long in its queue",
  stopped: "Stopped while waiting for the shared model",
};

const PERIOD_LABEL = { today: "Today", "7d": "Last 7 days", cycle: "This billing cycle", "30d": "Last 30 days", all: "All time" };
const PERIOD_IN = { today: "today", "7d": "in the last 7 days", cycle: "in this billing cycle", "30d": "in the last 30 days", all: "yet" };

function usageErrorsHtml(errors) {
  if (!errors || !errors.length) return "";
  const n = errors.reduce((a, e) => a + (e.calls || 0), 0);
  return `<div class="usage-errors" role="note"><span class="material-symbols-outlined msym" aria-hidden="true">error</span>` +
    `<div><strong>${plural(n, "failed request")}</strong><ul>` +
    errors.map((e) => `<li>${e.calls}× ${escapeHtml(ERROR_TEXT[e.error_type] || e.error_type)} <span class="muted">· last ${escapeHtml(fmtDate(e.last_at))}</span></li>`).join("") +
    "</ul></div></div>";
}

// The breakdown of the picked period: failed requests first, then one list at a time, by model,
// run or chat. A run and a chat open their conversation.
const usageView = { by: "model", data: null };

function usageBreakdownHtml(d, by = "model") {
  const p = (d.totals && d.totals.period) || {};
  if (!p.calls) {
    const when = PERIOD_IN[d.period] || "in this period";
    return `<p class="usage-empty"><strong>No model calls ${when}.</strong> ` +
      "Every chat reply and run step that calls a model is listed here, with its tokens and estimated cost." +
      (d.period !== "all" ? " Pick a longer period to see earlier ones." : "") + "</p>";
  }
  const views = [
    ["model", "Models", d.by_model, "Model", (x) => `<code>${escapeHtml(x.model)}</code>`, ""],
    ["run", "Runs", d.by_run, "Run", (x) => {
      const q = escapeHtml(x.question || x.run_id);
      const name = x.conversation_id ? `<a href="#" data-open-chat="${escapeHtml(String(x.conversation_id))}" title="Run ${escapeHtml(x.run_id)}: open its chat">${q}</a>` : `<span title="Run ${escapeHtml(x.run_id)}">${q}</span>`;
      return name + (x.status ? ` <span class="usage-status ${escapeHtml(x.status)}">${escapeHtml(x.status)}</span>` : "");
    }, "None of the calls in this period belonged to a run: they were chat replies."],
    ["chat", "Chats", d.by_chat, "Chat", (x) => {
      const t = x.title || chatTitle(x.conversation_id) || `chat ${x.conversation_id}`;
      return `<a href="#" data-open-chat="${escapeHtml(String(x.conversation_id))}">${escapeHtml(t)}</a>`;
    }, "None of the calls in this period belonged to a chat."],
  ];
  const cur = views.find((v) => v[0] === by) || views[0];
  const seg = `<div class="usage-seg" role="group" aria-label="Break down by">` + views.map(([k, label, rows]) =>
    `<button type="button" class="usage-seg-btn${k === cur[0] ? " active" : ""}" aria-pressed="${k === cur[0]}" data-usage-by="${k}">${label} <span class="usage-seg-n">${(rows || []).length}</span></button>`).join("") + "</div>";
  return usageErrorsHtml(d.errors) + seg + usageList(cur[2], cur[3], cur[4], { empty: cur[5], capped: 50 });
}

function usagePeriodTotalHtml(d) {
  const p = (d.totals && d.totals.period) || {};
  return `${escapeHtml(PERIOD_LABEL[d.period] || "")}: ${usageTotalText(p)}`;
}

// "~$2.47 · 2.4M API tokens · 86 API calls · 1 call on the cluster GPU (free)"
function usageTotalText(t) {
  const api = apiPart(t);
  const free = freeParts(t).map((f) => ` · ${plural(f.calls, "call")} on the ${lowerFirst(f.name)} (free)`);
  return `${fmtUsd(t.spend_usd)} · ${fmtTokens(api.total_tokens)} API tokens · ${plural(api.calls, "API call")}` + free.join("");
}

function usageNoteHtml(d) {
  return `<span class="material-symbols-outlined msym" aria-hidden="true">info</span><span>${escapeHtml(d.note || "")} ` +
    `<a href="${escapeHtml(d.console_url || "")}" target="_blank" rel="noopener">Open the Claude Console usage page</a>.` +
    (d.pricing && d.pricing.as_of ? ` Prices as of ${escapeHtml(fmtDay(d.pricing.as_of, { year: true }))}.` : "") + "</span>";
}

function renderUsage(d) {
  $("usageCycle").textContent = usageCycleText(d.budget && d.budget.cycle);
  $("usageBody").innerHTML = usageSummaryHtml(d) + usageChart(d.daily);
  $("usagePeriodTotal").innerHTML = usagePeriodTotalHtml(d);
  $("usageBreakdown").innerHTML = usageBreakdownHtml(d, usageView.by);
  $("usageNote").innerHTML = usageNoteHtml(d);
}

function usageFailedHtml(why) {
  return `<div class="usage-empty">Could not load your usage (${escapeHtml(why)}). ` +
    `<button type="button" class="secondary small" data-usage-retry>Try again</button></div>`;
}

async function loadUsage() {
  const body = $("usageBody");
  if (!body) return;
  const clear = () => { ["usageCycle", "usagePeriodTotal", "usageBreakdown", "usageNote"].forEach((id) => { $(id).innerHTML = ""; }); };
  if (!authed()) { clear(); body.innerHTML = '<p class="usage-empty">Usage tracking needs an account.</p>'; return; }
  const period = ($("usagePeriod") && $("usagePeriod").value) || "cycle";
  $("usageExport").href = `/api/usage/export.csv?period=${encodeURIComponent(period)}&tz=${encodeURIComponent(userTz())}`;
  try {
    const r = await fetch(`/api/usage?period=${encodeURIComponent(period)}&tz=${encodeURIComponent(userTz())}`);
    if (!r.ok) { clear(); body.innerHTML = usageFailedHtml(`HTTP ${r.status}`); return; }
    const d = await r.json();
    usageView.data = d;
    renderUsage(d);
    fillBudgetForm(d.budget && d.budget.budget);
  } catch (err) { clear(); body.innerHTML = usageFailedHtml(err.message); }
}

function fillBudgetForm(b) {
  if (!$("budgetForm") || $("budgetForm").dataset.dirty) return;
  $("budgetCredit").value = b && b.monthly_credit_usd != null ? b.monthly_credit_usd : "";
  $("budgetResetDay").value = (b && b.reset_day) || 1;
  $("budgetThresholds").value = ((b && b.thresholds) || [50, 80, 100]).join(", ");
  $("budgetHardLimit").value = b && b.hard_limit_usd != null ? b.hard_limit_usd : "";
  $("budgetHardMode").value = (b && b.hard_limit_mode) || "confirm";
}

async function saveBudget(e) {
  e.preventDefault();
  const num = (id) => { const v = $(id).value.trim(); return v === "" ? null : Number(v); };
  const thresholds = $("budgetThresholds").value.split(/[,\s]+/).map((x) => parseInt(x, 10)).filter((x) => x > 0);
  const payload = { monthly_credit_usd: num("budgetCredit"), reset_day: parseInt($("budgetResetDay").value, 10) || 1,
    thresholds, hard_limit_usd: num("budgetHardLimit"), hard_limit_mode: $("budgetHardMode").value };
  const r = await fetch("/api/usage/budget", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.detail || "Could not save the budget."); return; }
  delete $("budgetForm").dataset.dirty;
  toast("Budget saved.");
  loadUsage();
  checkBudgetNotices();
}

// A run's breakdown by pipeline step and model, under its card in Runs.
async function toggleRunUsage(btn) {
  const card = btn.closest(".run-card");
  let box = card && card.querySelector(".run-usage");
  if (box) { box.remove(); btn.textContent = "Usage"; return; }
  box = document.createElement("div");
  box.className = "run-usage";
  box.innerHTML = '<span class="muted small">Loading…</span>';
  card.appendChild(box);
  btn.textContent = "Hide usage";
  try {
    const d = await (await fetch(`/api/usage/run/${encodeURIComponent(btn.dataset.runUsage)}`)).json();
    box.innerHTML = runUsageHtml(d);
  } catch (err) { box.innerHTML = `<span class="bad small">Could not load: ${escapeHtml(err.message)}</span>`; }
}

function runUsageHtml(d) {
  if (!d.calls) return '<span class="muted small">No model calls were recorded for this run (runs before usage tracking have none).</span>';
  return `<p class="run-usage-total"><strong>Estimated</strong> ${usageTotalText(d)}</p>` +
    usageErrorsHtml(d.errors) +
    `<h4>By step</h4>${usageList(d.by_step, "Step", (x) => escapeHtml(x.step))}` +
    `<h4>By model</h4>${usageList(d.by_model, "Model", (x) => `<code>${escapeHtml(x.model)}</code>`)}`;
}

function adminUsageHtml(d) {
  const t = d.totals || {};
  const since = fmtDay(String(d.since || "").slice(0, 10), { year: true });
  const tiles = `<div class="usage-tiles">` +
    usageTile("Estimated API spend", fmtUsd(t.spend_usd), `Since ${escapeHtml(since)}`) + apiTokensTile(t) +
    usageTile("Accounts", String((d.by_user || []).length), "with at least one model call") + "</div>";
  const daily = (d.by_day || []).map((x) => ({ date: x.date, spend_usd: x.spend_usd, total_tokens: x.total_tokens, calls: x.calls }));
  return tiles + usageChart(daily) +
    `<h4>By account</h4>${usageList(d.by_user, "Account", (x) => escapeHtml(x.username))}` +
    `<h4>By model</h4>${usageList(d.by_model, "Model", (x) => `<code>${escapeHtml(x.model)}</code>`)}` +
    `<h4>By day</h4>${usageList([...(d.by_day || [])].filter((x) => x.calls).reverse(), "Day", (x) => escapeHtml(fmtDay(x.date, { year: true })))}`;
}

async function loadAdminUsage() {
  const el = $("adminUsageBody");
  if (!el) return;
  el.innerHTML = '<div class="empty-hint">Loading…</div>';
  try {
    const days = ($("adminUsageDays") && $("adminUsageDays").value) || 30;
    const r = await fetch(`/api/admin/usage?days=${encodeURIComponent(days)}&tz=${encodeURIComponent(userTz())}`);
    if (!r.ok) { el.innerHTML = '<div class="empty-hint">Admin only.</div>'; return; }
    el.innerHTML = adminUsageHtml(await r.json());
  } catch (err) { el.innerHTML = `<div class="empty-hint">Failed to load: ${escapeHtml(err.message)}</div>`; }
  loadAdminKeys();
}

// Admin › LLM usage › API keys: every key on the server (public rows only) with its last window's
// use, and Pause / Resume (key_guard, RCHENLAB/AiScientist issue 16).
async function loadAdminKeys() {
  const el = $("adminKeysBody");
  if (!el) return;
  try {
    const r = await fetch("/api/admin/llm-keys");
    if (!r.ok) { el.innerHTML = '<div class="empty-hint">Admin only.</div>'; return; }
    const d = await r.json();
    const keys = d.keys || [];
    if (!keys.length) { el.innerHTML = '<p class="usage-empty">No API keys on this server.</p>'; return; }
    const win = Math.round((d.spike || {}).window_min || 60);
    const rows = keys.map((k) => {
      const w = k.window || {};
      const use = w.calls == null ? "" : `${plural(w.calls, "call")} · ${fmtUsd(w.spend_usd || 0)} in the last ${win} min`;
      const by = { owner: "its owner", admin: "an administrator", auto: "the spike check" }[k.paused_by] || "";
      const st = k.paused_at ? `<span class="bad">paused by ${escapeHtml(by)}${k.pause_reason ? ` — ${escapeHtml(k.pause_reason)}` : ""}</span>` : "active";
      const btn = k.paused_at
        ? '<button type="button" class="ghost small" data-admin-key="resume">Resume</button>'
        : '<button type="button" class="ghost small" data-admin-key="pause">Pause</button>';
      return `<li class="usage-row" data-owner="${escapeHtml(k.owner)}" data-id="${escapeHtml(k.id)}"><div class="usage-row-main">` +
        `<div class="usage-row-name">${escapeHtml(k.owner)} · ${escapeHtml(k.label || "")}</div>` +
        `<div class="usage-row-meta">${escapeHtml(k.provider || "")} · key ${escapeHtml(k.key_hint || "")}` +
        `${k.session_only ? " · this session only" : ""} · ${st}${use ? ` · ${use}` : ""}</div></div>` +
        `<div class="usage-row-cost">${btn}</div></li>`;
    }).join("");
    el.innerHTML = `<div class="usage-list"><ul>${rows}</ul></div>` +
      (d.revoked_at ? `<p class="usage-foot">Every key was revoked at ${escapeHtml(new Date(d.revoked_at).toLocaleString())}.</p>` : "");
  } catch (err) { el.innerHTML = `<div class="empty-hint">Failed to load: ${escapeHtml(err.message)}</div>`; }
}

async function adminKeyAction(ev) {
  const btn = ev.target.closest("[data-admin-key]");
  if (!btn) return;
  const row = btn.closest("[data-id]");
  const act = btn.dataset.adminKey;
  let reason = "";
  if (act === "pause") {
    reason = window.prompt("Why? The key's owner reads this in the console and by email.", "");
    if (reason === null) return;
  }
  btn.disabled = true;
  try {
    const r = await fetch(`/api/admin/llm-keys/${act}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner: row.dataset.owner, credential_id: row.dataset.id, reason }),
    });
    const d = await r.json();
    toast(r.ok ? (act === "pause" ? "Paused; the owner was told." : "Resumed.") : (d.error || d.detail || "Failed"));
  } finally { btn.disabled = false; loadAdminKeys(); }
}

// Scroll to the budget form and put the cursor in its first field.
function editBudget() {
  const sec = $("budgetSection");
  sec.scrollIntoView({ behavior: "smooth", block: "start" });
  setTimeout(() => $("budgetCredit").focus({ preventScroll: true }), 300);
}

function wireUsage() {
  document.querySelectorAll("[data-guide]").forEach((a) => { a.href = GUIDE_URL; });
  $("accountKeysBtn").addEventListener("click", () => openApiKeys());
  $("usageRefresh").addEventListener("click", loadUsage);
  $("usagePeriod").addEventListener("change", loadUsage);
  $("budgetForm").addEventListener("submit", saveBudget);
  $("budgetForm").addEventListener("input", () => { $("budgetForm").dataset.dirty = "1"; });
  $("changePwForm").addEventListener("input", () => $("cpProblem").classList.add("hidden"));
  const tabs = [...document.querySelectorAll(".account-tab")];
  tabs.forEach((b) => {
    b.addEventListener("click", () => switchAccountTab(b.dataset.accountTab));
    // Arrow keys move between the tabs, as in any tab list.
    b.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
      const shown = tabs.filter((t) => !t.classList.contains("hidden"));
      const next = shown[(shown.indexOf(b) + (e.key === "ArrowRight" ? 1 : shown.length - 1)) % shown.length];
      switchAccountTab(next.dataset.accountTab); next.focus(); e.preventDefault();
    });
  });
  const onUsageClick = (e) => {
    const a = e.target.closest("[data-open-chat]");
    if (a) { e.preventDefault(); openChat(a.dataset.openChat); return; }
    if (e.target.closest("[data-budget-edit]")) { e.preventDefault(); editBudget(); return; }
    if (e.target.closest("[data-usage-retry]")) { loadUsage(); return; }
    const by = e.target.closest("[data-usage-by]");
    if (by && usageView.data) { usageView.by = by.dataset.usageBy; $("usageBreakdown").innerHTML = usageBreakdownHtml(usageView.data, usageView.by); }
  };
  $("usageBody").addEventListener("click", onUsageClick);
  $("usageBreakdown").addEventListener("click", onUsageClick);
  $("adminUsageRefresh").addEventListener("click", loadAdminUsage);
  $("adminKeysBody").addEventListener("click", adminKeyAction);
  $("adminUsageDays").addEventListener("change", loadAdminUsage);
  $("chatUsage").addEventListener("click", () => openAccount("usage"));
  $("apiKeysUsageLink").addEventListener("click", (e) => { e.preventDefault(); $("apiKeysModal").classList.add("hidden"); openAccount("usage"); });
  $("budgetNotice").addEventListener("click", (e) => {
    if (e.target.closest("[data-dismiss]")) { $("budgetNotice").classList.add("hidden"); return; }
    if (e.target.closest("[data-usage-open]")) { e.preventDefault(); openAccount("usage"); }
  });
}

// ---- init -----------------------------------------------------------------

function init() {
  $("connectForm").addEventListener("submit", connect);
  $("reattachDismiss").addEventListener("click", () => $("reattachHint").classList.add("hidden"));
  $("disconnectBtn").addEventListener("click", disconnect);
  $("stopGpuBtn").addEventListener("click", stopGpu);
  $("sessChip").addEventListener("click", openStorageSessions);
  $("storageRefresh").addEventListener("click", loadStorageTab);
  $("storeServerBody").addEventListener("click", onServerStorageClick);
  $("originalsBox").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-orig]");
    if (b) originalsAction(b.dataset.orig, b.dataset.plan);
  });
  $("storeHpcBody").addEventListener("click", (e) => { const b = e.target.closest("button[data-path]"); if (b) deleteStorageItem(b.dataset.path); });

  // --- LLM endpoint + API-key manager ---
  // "＋ Add your own API key…" is a menu ITEM, not an endpoint: it opens the manager and snaps the
  // picker back to whatever is actually in use, so the dropdown never shows a selection that isn't.
  $("llmEndpointSelect").addEventListener("change", (e) => {
    if (e.target.value === ADD_KEY_OPTION) {
      e.target.value = apiKeys.activeId || clusterValue(); syncEndpointActions(); openApiKeys(); return;
    }
    syncEndpointActions();
    selectLlmEndpoint(e.target.value);
  });
  $("manageKeysBtn").addEventListener("click", () => openApiKeys());
  $("manageClusterBtn").addEventListener("click", openClusterModels);
  $("connectManageClusterBtn").addEventListener("click", openClusterModels);
  $("clusterClose").addEventListener("click", () => { closeClusterForm(); $("clusterModal").classList.add("hidden"); });
  $("clusterModal").addEventListener("click", (e) => { if (e.target.id === "clusterModal") { closeClusterForm(); $("clusterModal").classList.add("hidden"); } });
  $("clusterList").addEventListener("click", clusterRowAction);
  $("clusterUnregistered").addEventListener("click", clusterRowAction);
  $("clusterAddBtn").addEventListener("click", () => fillClusterForm(null));
  $("clusterCancel").addEventListener("click", closeClusterForm);
  $("clusterForm").addEventListener("submit", saveClusterForm);
  $("editKeyBtn").addEventListener("click", () => editSelectedEndpoint("llmEndpointSelect"));
  $("deleteKeyBtn").addEventListener("click", () => deleteEndpointById($("llmEndpointSelect").value));
  // The same manager, reachable from the LOGIN form. Without this the only way to register a key
  // was the post-connect panel — i.e. after the GPU wait the key exists to avoid.
  $("connectManageKeysBtn").addEventListener("click", () => openApiKeys());
  $("connectEditKeyBtn").addEventListener("click", () => editSelectedEndpoint("connectLlmSelect"));
  $("connectDeleteKeyBtn").addEventListener("click", () => deleteEndpointById($("connectLlmSelect").value));
  $("connectLlmSelect").addEventListener("change", (e) => {
    if (e.target.value === ADD_KEY_OPTION) {
      e.target.value = apiKeys.connectChoice || ""; syncEndpointActions(); openApiKeys(); return;
    }
    apiKeys.connectChoice = e.target.value;
    renderConnectLlmHint();
    syncEndpointActions();
  });
  $("apiKeysClose").addEventListener("click", () => $("apiKeysModal").classList.add("hidden"));
  $("apiKeysModal").addEventListener("click", (e) => { if (e.target.id === "apiKeysModal") $("apiKeysModal").classList.add("hidden"); });
  $("apiKeyList").addEventListener("click", apiKeyRowAction);
  $("apiKeyForm").addEventListener("submit", submitApiKey);
  $("apiKeyCancel").addEventListener("click", resetApiKeyForm);
  $("apiKeyProvider").addEventListener("change", applyProviderPreset);
  $("apiKeyFetchModels").addEventListener("click", fetchEndpointModels);
  for (const [inputId, menuId] of MODEL_COMBOS) wireModelCombo(inputId, menuId);
  // Fetch the list the moment we have something to fetch it with, so clicking the model box just
  // shows it. `change` (not `input`) so this fires when the user has finished a field, not on
  // every keystroke of a half-typed key.
  for (const id of ["apiKeyBaseUrl", "apiKeyValue"]) {
    $(id).addEventListener("change", () => maybeAutoFetchModels());
  }
  // The consent dialog has no silent dismissal: closing it counts as declining, so a click on
  // the backdrop or ✕ snaps the picker back to the endpoint actually in use rather than leaving
  // the dropdown showing a selection that never took effect.
  $("egressAccept").addEventListener("click", () => closeEgressDialog(true));
  $("egressCancel").addEventListener("click", () => closeEgressDialog(false));
  $("egressClose").addEventListener("click", () => closeEgressDialog(false));
  $("egressModal").addEventListener("click", (e) => { if (e.target.id === "egressModal") closeEgressDialog(false); });
  $("duoPushBtn").addEventListener("click", () => submitDuo("1"));
  $("duoSubmitBtn").addEventListener("click", () => submitDuo($("duoInput").value.trim() || "1"));
  $("duoInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitDuo($("duoInput").value.trim() || "1"); } });
  $("authMethod").addEventListener("change", syncAuthMethod);
  $("createKeyCheck").addEventListener("change", syncNewKeyPass);
  // Re-load saved keys for whatever UCInetID is typed (keys are per-user).
  $("userInput").addEventListener("change", () => {
    if ($("authMethod").value === "ssh_key") loadCredentials();
    loadApiCredentials();   // credentials are per-owner, so the picker follows the UCInetID
  });
  loadApiCredentials();     // populate the login picker on first paint, before any session exists
  loadClusterModels();      // and its cluster-model options
  $("chatForm").addEventListener("submit", sendChat);
  $("chatStop").addEventListener("click", stopRun);
  $("datasetFile").addEventListener("change", (e) => { for (const f of e.target.files || []) uploadDataset(f); e.target.value = ""; });   // multi-attach: each file joins the bind-set
  $("datasetDir").addEventListener("change", (e) => { if (e.target.files && e.target.files.length) uploadFolder(e.target.files); e.target.value = ""; });
  $("datasetChips").addEventListener("click", (e) => {
    const main = e.target.closest("[data-main-path]");
    if (main) { makeMainDataset(main.dataset.mainPath); return; }
    const r = e.target.closest("[data-ref-path]");
    if (r) { toggleReference(r.dataset.refPath); return; }
    const x = e.target.closest("[data-clear-path]");
    if (x) toggleBound(x.dataset.clearPath);
  });
  // "＋ Data": Files or Folder, each straight to its dialog; or drop files and folders onto the chat.
  $("dataMenuBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleDataMenu(); });
  $("dataMenu").addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]");
    const target = act && { file: "datasetFile", folder: "datasetDir" }[act.dataset.act];
    if (target) $(target).click();
    if (act) toggleDataMenu(false);
  });
  wireDataDrop();
  document.addEventListener("click", (e) => {
    if (!e.composedPath().some((n) => n.classList && n.classList.contains("data-menu-wrap"))) toggleDataMenu(false);
  });
  $("downloads").addEventListener("click", (e) => {
    if (e.target.closest("[data-rerun]")) { primeComposer("rerun"); return; }
    const tab = e.target.closest("[data-rtab]");
    if (tab) { switchResultsTab(tab.dataset.rtab); return; }
    const cell = e.target.closest("[data-url]");
    if (cell) { e.preventDefault(); openResultFile(cell.dataset.url, cell.dataset.kind, cell.dataset.name); }
  });
  $("filePreviewClose").addEventListener("click", () => $("filePreviewModal").classList.add("hidden"));
  $("dsPreviewClose").addEventListener("click", closeDatasetPreview);
  $("dsPreviewBody").addEventListener("click", onDatasetPreviewClick);
  $("datasetPreviewModal").addEventListener("click", (e) => { if (e.target.id === "datasetPreviewModal") closeDatasetPreview(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("datasetPreviewModal").classList.contains("hidden")) closeDatasetPreview();
  });
  $("filePreviewModal").addEventListener("click", (e) => { if (e.target.id === "filePreviewModal") $("filePreviewModal").classList.add("hidden"); });
  $("chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chatForm").requestSubmit(); } });
  // Live-swap Send⇄Stop as the user types during a run / plan review; the text is this chat's draft.
  $("chatInput").addEventListener("input", () => {
    const c = activeChat();
    if (c) { c.draft = $("chatInput").value; saveComposer(c); }
    updateComposerButton();
  });
  $("serverPathOffer").addEventListener("click", (e) => {
    const b = e.target.closest("[data-spo-bind]");
    if (b) { bindServerPath(b.dataset.spoBind); return; }
    const x = e.target.closest("[data-spo-dismiss]");
    if (x) { state.serverOfferDismissed.add(x.dataset.spoDismiss); renderServerPathOffer(); }
  });
  $("newSessionBtn").addEventListener("click", newSession);
  $("sessionList").addEventListener("click", (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { e.stopPropagation(); deleteSession(del.dataset.del); return; }
    const item = e.target.closest("[data-id]");
    if (item) selectSession(item.dataset.id);
  });
  $("toggleLeft").addEventListener("click", () => togglePanel("left"));
  $("toggleRight").addEventListener("click", () => togglePanel("right"));
  $("sideTabPlan").addEventListener("click", () => { state.planView = null; selectSideTab("plan"); renderPlanPanel(); });
  $("sideTabResults").addEventListener("click", () => selectSideTab("results"));
  $("leftLogoutBtn").addEventListener("click", doLogout);

  syncAuthMethod();
  applyUiPrefs();
  loadSessions();
  renderSessionList();
  renderChat();
  applyStatus({ status: "disconnected", model: "" });
  renderDatasetChips();
  loadDatasetChips();    // show prior uploads (incl. folders) as selectable chips
  loadServerRoots();     // server folders that can be bound by path (Data menu + chat offer)
  restoreConnection();   // re-subscribe to a still-live HPC3 session after a reload / back-nav
}

// After a reload or accidental back-navigation the server-side Connection (SSH+GPU+vLLM)
// and any in-flight run usually SURVIVE — only the client forgot its id. If we stored one,
// check it's still alive and re-open the WebSocket, which replays the status, the event
// log, any pending Duo/Plan prompt, and the live centre bubble of an in-flight run.
async function restoreConnection() {
  let id = null;
  try { id = localStorage.getItem(CONN_KEY); } catch {}
  if (!id) {
    // A browser that never connected joins the session its account already has open elsewhere.
    try {
      const d = await (await fetch("/api/session")).json();
      id = d && d.connection_id;
    } catch { /* no accounts, or the server is unreachable: show the connect form */ }
    if (!id) return;
    try { localStorage.setItem(CONN_KEY, id); } catch {}
  }
  try {
    const res = await fetch(`/api/connections/${id}`);
    if (res.status === 401 || res.status === 403) {
      // Stored by another account on this browser (or before a logout): not ours to reattach to
      // or to advertise — drop it, run owner included.
      forgetConnection();
      return;
    }
    if (!res.ok) {
      // Connection is gone — almost always a gateway restart (e.g. a redeploy). The GPU
      // job on HPC3 usually OUTLIVES it, so reconnecting reattaches instead of re-queuing.
      try { localStorage.removeItem(CONN_KEY); } catch {}
      showReattachHint();
      return;
    }
    const summary = await res.json();
    if (!summary.status || summary.status === "disconnected") {
      try { localStorage.removeItem(CONN_KEY); } catch {}
      showReattachHint();
      return;
    }
    state.connectionId = id;
    // Re-bind each in-flight run to its chat BEFORE the WS replays it, so its replayed
    // report/artifacts land in the chat that launched it — not the active one.
    for (const sid of loadRunOwners(id)) chatOf(sid).owned = true;
    openWs(id);   // WS replay restores status + (if a run is live) the streaming bubble
  } catch { /* local server unreachable — stay on the disconnected/login view */ }
}

// After a gateway restart the in-memory connection is gone, but the user's Slurm GPU job
// + loaded model usually keep running on HPC3. Surface that so the user re-logs in (which
// reattaches automatically) instead of assuming everything was lost.
function showReattachHint() {
  let rec = null;
  try { rec = JSON.parse(localStorage.getItem(LASTCONN_KEY) || "null"); } catch {}
  if (!rec) return;
  // Another account's session on this browser: its UCInetID and job are not this user's business.
  if (state.user && rec.account !== state.user.username) return;
  // Prefill first and unconditionally — useful for re-login even when the GPU job is long gone.
  if (rec.username && $("userInput") && !$("userInput").value) $("userInput").value = rec.username;
  if (rec.host && $("hostInput")) $("hostInput").value = rec.host;

  const banner = $("reattachHint");
  if (!banner) return;
  // Do NOT claim a job is alive we cannot see. This banner is driven purely by a localStorage record
  // (no Slurm query is possible while disconnected), and it used to assert "is likely still running"
  // unconditionally — so a job that had already died (e.g. TIMEOUT at its --time wall limit, or one the
  // user scancel'd) kept being advertised as running forever. Two guards:
  //   1. past the wall limit the job CANNOT still exist — Slurm killed it — so show nothing;
  //   2. within the window we still only say "may", because it could also have been cancelled/crashed.
  // (doStopGpu() additionally clears the record outright, so a deliberate Stop never leaves a hint.)
  const limitMs = (rec.limitSec || 0) * 1000;
  if (!rec.jobId || !rec.ts || (limitMs && Date.now() - rec.ts >= limitMs)) return;

  const job = ` <code>${escapeHtml(String(rec.jobId))}</code>${rec.node ? " on " + escapeHtml(rec.node) : ""}`;
  const model = rec.model ? ` (${escapeHtml(rec.model)})` : "";
  $("reattachHintBody").innerHTML =
    `🟢 <strong>Your session ended (the server restarted).</strong> Your HPC3 GPU job${job}${model} ` +
    `<strong>may still be running</strong> — log in again to check; if it is, you will ` +
    `<strong>reattach automatically</strong> (skipping the GPU queue and model reload). ` +
    `Done with it? After connecting, use <em>Stop GPU</em> to free your SU.`;
  banner.classList.remove("hidden");
}

bootstrap();
