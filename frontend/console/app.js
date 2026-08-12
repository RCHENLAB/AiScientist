"use strict";

const SESSIONS_KEY = "bioagent_sessions_v1";
const UI_KEY = "bioagent_ui_v1";
const CONN_KEY = "bioagent_connection_v1";  // last live connection id — survives a reload so we can re-subscribe
const LASTCONN_KEY = "bioagent_lastconn_v1"; // summary of the last ready session — powers the "reattach" hint after a gateway restart
const RUNOWNER_KEY = "bioagent_runowner_v1"; // {connId, sid}: which chat owns the in-flight run — survives a reload so results never leak to the active chat
const LASTRUN_KEY = "bioagent_lastrun_v1";   // last COMPLETED run_id — survives a refresh so we can still recognise this conn's own run

const state = {
  connectionId: null,
  ws: null,
  status: "disconnected",
  lastJobId: null,
  lastNode: null,
  lastGres: null,
  sessions: [],
  activeId: null,
  runSessionId: null,   // the chat that launched the in-flight pipeline run (its OWNER)
  running: false,       // a chat/pipeline run is in flight
  deadRunTimer: null,   // grace timer that stops a "running" UI whose backend run died silently
  // In-flight run stream, kept DECOUPLED from the DOM so switching chats (or a reload)
  // never detaches it. The live bubble is (re)mounted only while the owner chat is
  // visible — see mountStream()/repaintStream(). `feed` is an ordered list of
  // {kind:"line",text,level} milestones and {kind:"code",code} step-code blocks.
  run: null,            // {text, thinking, feed:[], status} or null when idle
  streamDom: null,      // {body, think, thinkBody, feed} DOM refs while owner chat visible
  presets: {},                 // key -> {key, label, prompt, tools} from /api/presets (preset pipelines)
  presetOrder: [],             // preset-pipeline keys in load order (for the Advanced picker)
  skills: {},                  // name -> {name, summary} from /api/skills (atomic skills)
  skillOrder: [],              // atomic-skill names in load order (for the Advanced required-skills list)
  datasets: [],                // uploaded files/folders this session: {name, path, kind}
  uploads: [],                 // in-flight/failed uploads shown in the Datasets tab: {id,name,size,sent,status,file,error}
  boundPaths: [],              // the BIND-SET (feature ②): the server paths of every data file attached
                               // for the next run (a VCF + a BED panel + a 2nd VCF). Ordered by attach;
                               // the server re-ranks to pick the primary. Posted as `datasets`.
  datasetPath: null,           // the PRIMARY of the bind-set (highest-ranked) — mirrored into the hidden
                               // datasetInput and posted as the legacy `dataset_path` for back-compat.
  caseNote: null,              // the attached clinical description: {name, text} — the SECOND
                               // attachment. Held as TEXT, not uploaded: a run binds exactly one
                               // dataset (which must be the VCF), and the note's only consumer
                               // (map_phenotype_to_hpo) runs on the gateway, not in a Slurm job.
  lastRunId: null,             // last completed run on this conn (used to recognise our own run after a refresh)
  leftAutoCollapsed: false,    // collapse the connect panel once, on first ready
  planPending: false,          // a PI plan/clarify is awaiting the user's decision
};

const $ = (id) => document.getElementById(id);

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
    .replace(/^### (.+)$/gm, "<h3>$1</h3>")
    .replace(/^## (.+)$/gm, "<h2>$1</h2>")
    .replace(/^# (.+)$/gm, "<h2>$1</h2>")
    // group consecutive "- " / "* " lines into a real bullet list
    .replace(/(?:^[ \t]*[-*] .+(?:\n|$))+/gm, (block) =>
      "<ul>" + block.trim().split(/\n/).map((l) =>
        "<li>" + l.replace(/^[ \t]*[-*] /, "") + "</li>").join("") + "</ul>")
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    // italic: underscores NOT flanked by word chars, so snake_case identifiers stay intact
    .replace(/(?<![A-Za-z0-9_])_([^_\n]+?)_(?![A-Za-z0-9_])/g, "<em>$1</em>");
  return withSymbols(s).replace(/￼C(\d+)￼/g, (_m, i) => codes[+i]);
}

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
  if (!r.ok) throw new Error(`${method} ${url} -> ${r.status}`);
  return r.status === 204 ? null : r.json();
}

// Map a stored server message row -> the in-memory shape messageEl() renders.
function msgFromServer(m) {
  if (m.kind === "artifacts") {
    const meta = m.meta || {};
    return { role: m.role || "assistant", kind: "artifacts", items: meta.items || [], bundleUrl: meta.bundleUrl };
  }
  const msg = { role: m.role, content: m.content };
  // Restore the collapsed run recap (thinking + steps/code) so a finished run stays
  // reviewable after a reload / on another device — not only in a downloaded log.
  const meta = m.meta || {};
  if (meta.thinking || (meta.feed && meta.feed.length)) {
    msg.thinking = meta.thinking || "";
    msg.feed = meta.feed || [];
  }
  return msg;
}

// The payload to persist a message server-side (artifacts: text empty, blobs by URL in meta).
function serverMsgPayload(msg) {
  if (msg.kind === "artifacts") {
    return { role: msg.role || "assistant", content: "", kind: "artifacts",
             meta: { items: msg.items || [], bundleUrl: msg.bundleUrl } };
  }
  const payload = { role: msg.role, content: msg.content || "" };
  if (msg.thinking || (msg.feed && msg.feed.length)) {
    payload.kind = "text";
    payload.meta = { thinking: msg.thinking || "", feed: msg.feed || [] };
  }
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
    s.presetKeys = String(data.conversation.preset_key || "").split(",").map((x) => x.trim()).filter(Boolean);  // restore forced skills
  } catch { /* keep whatever we have */ }
  s.loaded = true;
}

async function loadSessions() {
  if (authed()) {
    try {
      const data = await api("GET", "/api/conversations");
      state.sessions = (data.conversations || []).map((c) => ({
        id: String(c.id), cid: c.id, title: c.title, messages: [], loaded: false, createdAt: c.created_at,
      }));
      if (!state.sessions.length) { await newSession(); }
      else { state.activeId = state.sessions[0].id; await ensureMessages(activeSession()); }
      renderSessionList();
      renderChat();
      return;
    } catch { /* fall back to localStorage on any server error */ }
  }
  try {
    const raw = JSON.parse(localStorage.getItem(SESSIONS_KEY) || "[]");
    state.sessions = Array.isArray(raw) ? raw : [];
  } catch { state.sessions = []; }
  if (!state.sessions.length) newSession();
  else state.activeId = state.sessions[0].id;
}

function persistSessions() {
  if (authed()) return;   // server-backed: every change is synced via its own endpoint
  try { localStorage.setItem(SESSIONS_KEY, JSON.stringify(state.sessions.slice(0, 50))); } catch {}
}

function activeSession() {
  return state.sessions.find((s) => s.id === state.activeId) || null;
}

async function newSession() {
  if (authed()) {
    try {
      const c = (await api("POST", "/api/conversations", {})).conversation;
      state.sessions.unshift({ id: String(c.id), cid: c.id, title: c.title, messages: [], loaded: true, createdAt: c.created_at });
      state.activeId = String(c.id);
      hideContextMeter();
      renderSessionList();
      renderChat();
      return;
    } catch { /* fall through to a local-only session */ }
  }
  const s = { id: rid(), title: "New chat", messages: [], loaded: true, createdAt: Date.now() };
  state.sessions.unshift(s);
  state.activeId = s.id;
  hideContextMeter();
  persistSessions();
  renderSessionList();
  renderChat();
}

async function selectSession(id) {
  state.activeId = id;
  // Occupancy is per-conversation and only known once THIS chat takes a turn — leaving the
  // previous conversation's number up would be worse than showing nothing.
  hideContextMeter();
  renderSessionList();
  await ensureMessages(activeSession());
  renderChat();
}

// ---- research path / methodology context ----------------------------------
// A preset steers the PI's plan via an editable guidance prompt (it does not bypass
// the PI). The prompt is conversation-level context: shown, editable, sent with the
// run, and persisted with the chat (server-side) so it survives a reload.

async function loadPresets() {
  try {
    const data = await (await fetch("/api/presets")).json();
    state.presets = {};
    state.presetOrder = [];
    for (const p of (data.presets || [])) { state.presets[p.key] = p; state.presetOrder.push(p.key); }
  } catch { /* presets are optional */ }
  renderPresetList($("presetSearch") ? $("presetSearch").value : "");
}

async function loadSkills() {
  try {
    const data = await (await fetch("/api/skills")).json();
    state.skills = {};
    state.skillOrder = [];
    for (const s of (data.skills || [])) { state.skills[s.name] = s; state.skillOrder.push(s.name); }
  } catch { /* skills are optional */ }
  renderSkillList();
}

// Render the atomic-skill checklist (Advanced panel). Ticking a skill REQUIRES it — the run must
// apply that capability. Skills compose (pick several); none ticked = the agent fetches on demand.
function renderSkillList() {
  const box = $("skillList");
  if (!box) return;
  const s = activeSession();
  const sel = new Set((s && s.skillKeys) || []);
  box.innerHTML = "";
  const names = state.skillOrder || [];
  if (!names.length) { box.innerHTML = '<div class="preset-empty">No atomic skills.</div>'; return; }
  for (const name of names) {
    const sk = state.skills[name] || {};
    const row = document.createElement("label");
    row.className = "preset-item" + (sel.has(name) ? " checked" : "");
    row.title = sk.summary || "";
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = name; cb.checked = sel.has(name);
    cb.addEventListener("change", () => onSkillToggle(name, cb.checked));
    const label = document.createElement("span");
    label.className = "preset-item-label";
    label.textContent = name.replace(/\.py$/, "").replace(/_/g, " ");
    row.appendChild(cb); row.appendChild(label);
    box.appendChild(row);
  }
}

// Require specific atomic skills for this chat's runs. Persisted per-chat (localStorage) like the
// mode; the run sends the checked names and the plan must apply each.
function onSkillToggle(name, checked) {
  const s = activeSession();
  if (!s) return;
  const set = new Set(s.skillKeys || []);
  checked ? set.add(name) : set.delete(name);
  s.skillKeys = [...set];
  const box = $("skillList");
  if (box) {
    const row = [...box.querySelectorAll(".preset-item")].find((r) => r.querySelector("input").value === name);
    if (row) row.classList.toggle("checked", checked);
  }
  persistSessions();
}

// Render the skill checklist (Advanced panel), filtered by the search box `q`, ticking the skills
// the active chat has selected. Ticking FORCES a skill into this conversation's plan; none ticked =
// the PI auto-selects. Multiple ticks are composed server-side into one guidance block.
function renderPresetList(q) {
  const box = $("presetList");
  if (!box) return;
  const s = activeSession();
  const sel = new Set((s && s.presetKeys) || []);
  const needle = (q || "").trim().toLowerCase();
  const keys = (state.presetOrder || []).filter((k) => {
    if (!needle) return true;
    const p = state.presets[k] || {};
    return (k + " " + (p.label || "") + " " + ((p.tools || []).join(" "))).toLowerCase().includes(needle);
  });
  box.innerHTML = "";
  if (!keys.length) { box.innerHTML = '<div class="preset-empty">No matching preset pipelines.</div>'; return; }
  for (const k of keys) {
    const p = state.presets[k];
    const row = document.createElement("label");
    row.className = "preset-item" + (sel.has(k) ? " checked" : "");
    const cb = document.createElement("input");
    cb.type = "checkbox"; cb.value = k; cb.checked = sel.has(k);
    cb.addEventListener("change", () => onPresetToggle(k, cb.checked));
    const label = document.createElement("span");
    label.className = "preset-item-label"; label.textContent = p.label || k;
    row.appendChild(cb); row.appendChild(label);
    box.appendChild(row);
  }
}

// Reflect the active chat's saved research path into the controls.
function syncPresetUI() {
  // Reflect the active chat's picks (preset pipelines + required skills) and run mode into the UI.
  renderPresetList($("presetSearch") ? $("presetSearch").value : "");
  renderSkillList();
  const s = activeSession();
  const modeSel = $("modeSelect");
  if (modeSel) modeSel.value = (s && s.mode) || "auto";
  const routeSel = $("routeSelect");
  if (routeSel) routeSel.value = (s && s.route) || "research";
  applyRouteUI();
}

// The user picks the MODE (single agent vs Virtual-Lab team); the PI still auto-selects
// the skill. Saved per-chat so switching chats restores the choice.
function onModeChange() {
  const s = activeSession();
  if (!s) return;
  s.mode = $("modeSelect").value || "auto";
  persistSessions();
}

// ---- Axis B: chat (fast path) vs research (full lab) ----------------------
// Which ENGINE answers, independent of Axis A (`mode`, one scientist vs a Virtual-Lab team,
// which only applies inside the research engine). Saved per-chat like `mode`, so a chat used
// for quick questions stays in chat mode when you switch back to it.
function activeRoute() {
  const s = activeSession();
  return (s && s.route) || ($("routeSelect") && $("routeSelect").value) || "research";
}

// The research-only controls (team mode, plan review, bypass) have no meaning on the fast path —
// there is no plan to review and no team to convene. Disable rather than hide, so the user can see
// what switching back to Research would give them.
function applyRouteUI() {
  const chat = activeRoute() === "chat";
  for (const id of ["modeSelect", "planMode", "autonomousMode"]) {
    const el = $(id);
    if (el) { el.disabled = chat; if (el.closest("label")) el.closest("label").classList.toggle("dimmed", chat); }
  }
  const sel = $("modeSelect");
  if (sel) sel.classList.toggle("dimmed", chat);
  const input = $("chatInput");
  if (input && !state.planPending) {
    input.placeholder = chat
      ? "Ask a quick question — answers start immediately (no analysis, no report)…"
      : DEFAULT_PLACEHOLDER;
  }
}

function onRouteChange() {
  const s = activeSession();
  if (!s) return;
  s.route = $("routeSelect").value || "research";
  persistSessions();
  applyRouteUI();
}

// Optional override: force specific research path(s) (the PI auto-selects by default). We persist
// only the KEYS — each skill's guidance body is PI-internal and never shown/edited; to refine the
// plan the user talks to the PI in chat (plan mode), not by editing text.
function onPresetToggle(key, checked) {
  const s = activeSession();
  if (!s) return;
  const set = new Set(s.presetKeys || []);
  checked ? set.add(key) : set.delete(key);
  s.presetKeys = [...set];
  const box = $("presetList");   // update the row's checked styling without a full re-render
  if (box) {
    const row = [...box.querySelectorAll(".preset-item")].find((r) => r.querySelector("input").value === key);
    if (row) row.classList.toggle("checked", checked);
  }
  saveSessionContext(s);
  persistSessions();
}

// Persist the research-path context onto the server conversation (debounced), so it
// survives a reload/device change — it's part of the chat's stored context.
let _ctxSaveTimer = null;
function saveSessionContext(s) {
  if (!authed() || !s || !s.cid) return;
  clearTimeout(_ctxSaveTimer);
  _ctxSaveTimer = setTimeout(() => {
    api("PATCH", `/api/conversations/${s.cid}`, {
      preset_key: (s.presetKeys || []).join(","), context_prompt: "",
    }).catch(() => {});
  }, 500);
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
  // If this chat owns the in-flight run, stop it first so the GPU/compute it's
  // holding is released — a run left orphaned by a deleted chat keeps consuming.
  if (state.running && state.runSessionId === id) {
    stopRun();
    state.runSessionId = null;
    setRunning(false);
  }
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
  if (state.activeId === id) {
    if (state.sessions.length) { state.activeId = state.sessions[0].id; await ensureMessages(activeSession()); }
    else { await newSession(); return; }
  }
  persistSessions();
  renderSessionList();
  renderChat();
}

function renderSessionList() {
  const list = $("sessionList");
  if (!state.sessions.length) { list.innerHTML = '<div class="session-empty">No chats yet.</div>'; return; }
  list.innerHTML = state.sessions
    .map((s) => `
      <div class="session-item ${s.id === state.activeId ? "active" : ""}" data-id="${s.id}">
        <span class="session-title">${escapeHtml(s.title || "New chat")}</span>
        <button class="session-del" data-del="${s.id}" title="Delete chat" aria-label="Delete chat">✕</button>
      </div>`)
    .join("");
}

// ---- chat rendering -------------------------------------------------------

function renderChat() {
  const s = activeSession();
  $("chatTitle").textContent = s ? s.title || "New chat" : "New chat";
  const stream = $("chatStream");
  stream.innerHTML = "";
  // Always re-derive the downloads panel from the ACTIVE session — including the
  // empty-session case below, which used to early-return and leave the previous
  // session's downloads stranded in the panel.
  refreshDownloads();
  syncPresetUI();   // reflect this chat's research-path context in the controls
  if (!s || (!s.messages.length && !ownerVisible())) {
    stream.innerHTML = '<div class="empty-hint">Connect to a GPU-backed Qwen3 on HPC3, then ask a research question.</div>';
    return;
  }
  for (const m of s.messages) stream.appendChild(messageEl(m));
  // If an in-flight run belongs to THIS chat, (re)mount its live bubble and repaint it
  // from state — so switching back to a running chat resumes the live stream instead of
  // showing a frozen/blank bubble.
  mountStream();
  stream.scrollTop = stream.scrollHeight;
}

function refreshDownloads() {
  const s = activeSession();
  const last = s && [...s.messages].reverse().find((m) => m.kind === "artifacts");
  if (last) renderDownloads(last.items, last.bundleUrl);
  else $("downloads").innerHTML = '<div class="downloads-empty">Run a pipeline to get a results bundle (.zip) and browse the report, figures, and tables here.</div>';
}

function messageEl(m) {
  if (m.kind === "artifacts") {
    const el = document.createElement("div");
    el.className = "artifacts-block";
    const bundle = m.bundleUrl ? `<a class="artifact-bundle" href="${m.bundleUrl}" download>${icon("download")}Download all results (.zip)</a>` : "";
    const n = (m.items && m.items.length) || 0;
    el.innerHTML = `<div class="artifacts-title">${icon("folder_open")}<span>Results &amp; code (${n})</span></div>` + bundle +
      `<div class="artifacts-hint">Browse &amp; preview every file — organized by folder — in the Downloads panel →</div>`;
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

function pushMessage(msg) {
  const s = activeSession();
  if (!s) return;
  s.messages.push(msg);
  if (msg.role === "user" && (s.title === "New chat" || !s.title)) {
    s.title = msg.content.slice(0, 42);
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

// The in-flight run OWNS a session id (state.runSessionId). We only ever paint its live
// bubble into the DOM while that owner chat is the visible one — so switching to another
// chat can never see (or steal) another chat's run, and switching BACK re-mounts and
// repaints from state (fixes the "frozen stream" + the cross-chat result leak).
function ownerVisible() { return state.run && state.runSessionId && state.runSessionId === state.activeId; }

function startAssistantStream() {
  state.run = { text: "", thinking: "", feed: [], status: "running", startedAt: Date.now() };
  state.streamDom = null;
  mountStream();
  // Heartbeat: keep the "Working…" timer ticking even when no events arrive for a
  // while (long HPC3 compute steps are silent), so the run never looks frozen.
  if (state.runTimer) clearInterval(state.runTimer);
  state.runTimer = setInterval(() => { if (ownerVisible()) repaintWorking(); }, 1000);
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

// Repaint the live bubble from state.run (idempotent full render — cheap; feeds are short).
function repaintStream() {
  const dom = state.streamDom;
  const run = state.run;
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
  const dom = state.streamDom, run = state.run;
  if (!dom || !dom.working || !run) return;
  if (run.status !== "running") { dom.working.style.display = "none"; return; }
  const secs = Math.floor((Date.now() - (run.startedAt || Date.now())) / 1000);
  // Prefer the REAL backend activity (model_call / tool_start) over a generic label.
  const label = run.text ? "Writing…" : (run.statusLabel || "Working…");
  // After a long silent stretch, reassure that compute is running (not stuck).
  const hint = secs >= 30 && !run.text ? " · this can take a few minutes" : "";
  dom.working.style.display = "flex";
  dom.workingText.textContent = `${label} · ${fmtElapsed(secs * 1000)}${hint}`;
}

// ---- stream event ingest (DOM-independent; always updates state) -----------
function streamToken(token) { if (state.run) { state.run.text += token; if (ownerVisible()) scheduleRepaint(); } }
function streamThinking(token) { if (state.run) { state.run.thinking += token; if (ownerVisible()) scheduleRepaint(); } }
function streamProgress(msg) { if (state.run) { state.run.feed.push({ kind: "line", text: msg.text || "", level: msg.level || "info" }); if (ownerVisible()) scheduleRepaint(); } }
// Transient "what's happening right now" label from the backend (model_call / tool_start).
// Drives the live working line so it shows real activity, not a generic "Working…".
function streamStatus(text) { if (state.run) { state.run.statusLabel = text || ""; if (ownerVisible()) repaintWorking(); } }
function streamCode(code) {
  if (!state.run) return;
  // The last successful run_code of a step wins: drop the previous trailing code block.
  const f = state.run.feed;
  if (f.length && f[f.length - 1].kind === "code") f.pop();
  f.push({ kind: "code", code: code || "" });
  if (ownerVisible()) scheduleRepaint();
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

function finishAssistantStream() {
  if (state.runTimer) { clearInterval(state.runTimer); state.runTimer = null; }
  if (!state.run) { clearRunOwner(); return; }
  const owner = state.runSessionId || state.activeId;
  const run = state.run;
  // Persist the finished report to the OWNER chat (which may no longer be active), and
  // KEEP the run recap with it (thinking + steps/code) — collapsed, for later review —
  // instead of discarding it when the live bubble is replaced.
  if (run.text || run.thinking || run.feed.length) {
    pushToSession(owner, {
      role: "assistant",
      content: run.text || "",
      thinking: run.thinking || "",
      feed: run.feed.slice(),
    });
  }
  const wasVisible = ownerVisible();
  state.run = null;
  state.streamDom = null;
  clearRunOwner();
  // If the owner chat is on screen, rebuild it from persisted messages so the live
  // (ephemeral) bubble is replaced by the saved final message cleanly.
  if (wasVisible) renderChat();
}

function clearRunOwner() { try { localStorage.removeItem(RUNOWNER_KEY); } catch {} }

function appendArtifacts(items, bundleUrl) {
  if (!items || !items.length) return;
  const owner = state.runSessionId || state.activeId;
  const msg = { role: "assistant", kind: "artifacts", items, bundleUrl };
  pushToSession(owner, msg);
  // Only touch the visible chat + downloads panel if the run's own chat is the one
  // on screen — otherwise these would bleed into whatever chat the user switched to.
  if (owner === state.activeId) {
    $("chatStream").appendChild(messageEl(msg));
    stickToBottom();
    renderDownloads(items, bundleUrl);
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
  const regen = `<button type="button" class="dl-regen" data-regen title="Ask the PI to revise the report (no re-run). Click, then tell it what to change in the chat — e.g. 'make the discussion concise'. Just say 'regenerate' to rebuild as-is.">${icon("autorenew")}<span>Regenerate report</span></button>`;
  const rerun = `<button type="button" class="dl-regen" data-rerun title="Ask the PI to re-run an analysis step. Click, then say what to change in the chat — e.g. 'redo clustering at resolution 1.0'; the PI picks the right step, re-runs it (and everything after), and updates the report.">${icon("replay")}<span>Re-run a step</span></button>`;
  d.innerHTML =
    `<div class="results-bar">${zip}${regen}${rerun}<div class="results-tabs">` +
      `<button type="button" class="rtab-btn active" data-rtab="files">${icon("folder_open")}<span>Files</span></button>` +
      `<button type="button" class="rtab-btn" data-rtab="preview">${icon("visibility")}<span>Preview</span></button>` +
    `</div></div>` +
    `<div class="rtab-pane active" data-pane="files"><div id="fileTree" class="file-tree"><div class="preview-empty">Loading…</div></div></div>` +
    `<div class="rtab-pane" data-pane="preview"><div id="filePreviewPane" class="file-preview-pane">` +
      `<div class="preview-empty">Select a code / text file under Files to preview it here.</div></div></div>`;
  const m = (bundleUrl || (items && items[0] && items[0].url) || "").match(/\/api\/(?:bundle|file|artifacts)\/([^/]+)\/([^/]+)/);
  if (m) {
    loadResults(m[1], m[2]);
    // This run's id — the target of "Regenerate report". Persisted so a refresh keeps regenerate working.
    state.lastRunId = m[2];
    try { localStorage.setItem(LASTRUN_KEY, m[2]); } catch {}
  }
}

// The results-panel "Regenerate report" / "Re-run a step" buttons are conversational entry points,
// not mechanical actions. Clicking one focuses the composer and hints what to say; the message the
// user then sends is routed by the PI's follow-up router (gateway `_dispatch_lab`): it classifies
// edit-report vs re-run-step — inferring WHICH step from the wording — and executes, updating the
// report. So the user never hand-picks a step number; they just tell the PI what they want.
function primeComposer(kind) {
  if (!state.connectionId) { toast("Connect to a session first"); return; }
  if (state.running) { toast("A task is running — let it finish first"); return; }
  const input = $("chatInput");
  if (!input) return;
  if (kind === "rerun") {
    input.placeholder = "Tell the PI what to change — e.g. “redo clustering at resolution 1.0”. It'll re-run the right step and update the report.";
    toast("Say what to change — the PI re-runs the right step.");
  } else {
    input.placeholder = "Tell the PI how to revise the report — e.g. “make the discussion concise”. Say “regenerate” to rebuild as-is.";
    toast("Say how to revise the report — the PI regenerates it.");
  }
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
  return (n / 1024 / 1024).toFixed(1) + " MB";
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
            `<span class="file-row-name">${escapeHtml(f.name)}</span>` +
            `<span class="file-row-size">${fmtSize(f.size)}</span></button>`
          ).join("");
        return `<details class="file-folder" open><summary>${meta(k).label} <span class="file-folder-count">${groups[k].length}</span></summary>${rows}</details>`;
      }).join("");
  } catch { /* preview is best-effort */ }
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

function applyStatus(summary) {
  const prevStatus = state.status;
  state.status = summary.status;
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
  const isProvisioning = summary.status === "connecting" || summary.status === "provisioning";
  // Full provisioning pipeline view while the session is coming up for the first time; once it
  // has been live, keep the chat on screen and report progress inline instead.
  const showProvisionView = isProvisioning && !state.everConnected;
  const showConnected = isReady || (isProvisioning && state.everConnected);
  if (isLogin && state.running) setRunning(false);   // only a lost/failed connection kills a run
  // Restore the "running" UI after a reload/reconnect: the server says a run is still in
  // flight, so show Stop even before the first replayed token arrives.
  if (showConnected && summary.chat_running && !state.running) setRunning(true);
  // Reconnect/reload restore of the run OWNER. The server is the source of truth for which
  // conversation owns the in-flight run (summary.active_run). A WS reconnect / page reload wipes
  // state.runSessionId, after which the Stop button POSTs the wrong conversation_id and the server
  // (correctly, for cross-window isolation) no-ops it — the run then runs on to its Slurm --time. If
  // THIS window is viewing the owner conversation, re-adopt it so Stop targets the right run. Gated on
  // activeId === the run's conversation so only the owner window claims it — another window never does.
  if (showConnected && summary.chat_running && summary.active_run &&
      summary.active_run.conversation_id &&
      state.activeId === summary.active_run.conversation_id &&
      state.runSessionId !== summary.active_run.conversation_id) {
    state.runSessionId = summary.active_run.conversation_id;
  }
  // Silent-death reconciliation: the frontend still thinks a run is live but the server reports NO
  // active run (chat_running=false). A run that FINISHED replays its terminal event (run_complete /
  // chat_error) which clears `running`; a run that DIED (backend restarted / GPU job reclaimed) leaves
  // no terminal event, so the UI would spin forever. Arm a short grace timer — if a replayed terminal
  // event clears `running` first it cancels the timer (via setRunning); otherwise the run died, so stop
  // waiting and say so instead of hanging.
  else if (showConnected && !summary.chat_running && state.running && !state.deadRunTimer) {
    state.deadRunTimer = setTimeout(() => {
      state.deadRunTimer = null;
      if (state.running) {
        finishAssistantStream();
        setRunning(false);
        toast("The previous run ended with no result — the backend restarted or the job was reclaimed.");
      }
    }, 2500);
  }
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
    $("connUser").textContent = `${summary.username || "you"} @ ${(summary.host || "").split(".")[0]}`;
    // The non-ready branch is only reachable after the session HAS been live (showConnected keeps
    // the chat up while it comes back), so say what's happening rather than naming a model.
    $("connSub").textContent = isReady
      ? `${summary.selected_model || summary.model}${summary.mock ? " · mock" : ""}`
      : "Bringing the GPU session up…";
  }
  if (isReady) {
    renderModelOptions(summary);
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

function renderModelOptions(summary) {
  const select = $("modelSelect");
  const models = summary.available_models || [];
  const selected = summary.selected_model || summary.model;
  select.innerHTML = models.map((m) => `<option value="${escapeHtml(m)}" ${m === selected || m.split(":")[0] === (selected || "").split(":")[0] ? "selected" : ""}>${escapeHtml(m)}</option>`).join("");
  if (!models.length) select.innerHTML = `<option>${escapeHtml(selected || "qwen3")}</option>`;
}

async function selectModel(model) {
  if (!state.connectionId || !model) return;
  try {
    const res = await fetch("/api/model", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connection_id: state.connectionId, model }) });
    const d = await res.json();
    if (!res.ok) { toast(d.error || "Model switch failed"); return; }
    if (d.status === "pulling") toast(`Pulling ${model} … watch the log for progress.`);
    else toast(`Model: ${model}`);
  } catch (err) { toast(isUnreachable(err) ? "Can't reach the local server." : "Model switch failed: " + err.message); }
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
  ws.onopen = () => { if (state.wsEpoch === epoch) state.wsRetries = 0; };
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
  ws.onclose = () => {
    // The server-side Connection + run task OUTLIVE a dropped socket (network blip,
    // laptop sleep, idle timeout). Reconnect with backoff so the WS endpoint replays:
    // continuing a live run, or RECOVERING one that finished while we were offline —
    // no manual page refresh needed.
    if (state.wsEpoch !== epoch || state.connectionId !== id) return;
    const delay = Math.min(1000 * 2 ** (state.wsRetries || 0), 15000);
    state.wsRetries = (state.wsRetries || 0) + 1;
    setTimeout(() => { if (state.wsEpoch === epoch && state.connectionId === id) openWs(id); }, delay);
  };
  state.ws = ws;
}

// Stream-family events that belong to the in-flight (or recovering) assistant turn.
// When we suppress a duplicate finished-run replay, every one of these is a no-op
// until the next chat_start clears the flag.
const STREAM_TYPES = new Set([
  "chat_token", "chat_thinking", "lab_progress", "run_status", "step_code",
  "chat_done", "chat_stopped", "chat_error", "artifacts", "run_complete",
]);

// Run-SCOPED events (a specific run's bubble, plan card, or decision prompt). One SSH/GPU
// Connection is shared across a user's windows/tabs, so the server tags every one of these with
// the conversation that OWNS the run. A window only processes the run IT launched
// (state.runSessionId) and drops the rest — so another window's run never streams into, cancels,
// or shows a plan card in this one.
const RUN_SCOPED = new Set([...STREAM_TYPES,
  "plan_prompt", "plan_clarify", "decision_prompt", "plan_done",
  // Context occupancy belongs to the conversation that asked, so another window's chat turn
  // never rewrites this one's meter. Deliberately NOT in STREAM_TYPES: it is a standing
  // status, not part of the assistant bubble being replayed.
  "chat_context"]);

// A tagged event for a conversation this client did NOT launch a run for. Untagged legacy events
// (no conversation_id) are never foreign, so a single-window session behaves exactly as before.
function foreignRun(msg) {
  return !!msg.conversation_id && msg.conversation_id !== state.runSessionId;
}

function handleWsMessage(msg) {
  if (msg.type === "chat_start") {
    // Ignore a run another window started on this shared session (foreignRun). Also drop a
    // duplicate replay of a run we already have: `recover` = the server is replaying a run that
    // already FINISHED, for a client that may have missed its live completion; if we already have
    // it, drop the whole replay so we don't duplicate the persisted report/recap. Otherwise open
    // the live bubble for OUR run (or recover a missed one into its owner chat + downloads).
    state.suppressStream = foreignRun(msg) || !!(msg.recover && alreadyHaveRun(msg.run_id));
    if (state.suppressStream) return;
    setRunning(true); startAssistantStream();
    if (state.run) state.run.conversationId = msg.conversation_id || state.runSessionId;
    return;
  }
  // Never let another window's run-scoped events touch our bubble / plan card / composer.
  if (foreignRun(msg) && RUN_SCOPED.has(msg.type)) return;
  if (state.suppressStream && STREAM_TYPES.has(msg.type)) return;
  switch (msg.type) {
    case "status": applyStatus(msg.connection); break;
    case "event": appendLog(msg); updatePipelineFromEvent(msg); updateBoot(msg); break;
    case "gpu_health": renderGpu({ gpu: { health: msg.health, job_id: state.lastJobId, node: state.lastNode, gres: state.lastGres } }); break;
    case "chat_token": streamToken(msg.token); break;
    case "chat_thinking": streamThinking(msg.token); break;
    case "lab_progress": streamProgress(msg); break;
    case "run_status": streamStatus(msg.text); break;
    case "step_code": streamCode(msg.code); break;
    case "chat_context": renderContextMeter(msg); break;
    case "chat_done": finishAssistantStream(); setRunning(false); break;
    case "chat_stopped":
      if (state.run && !state.run.text) state.run.text = "_⏹ Stopped._";
      finishAssistantStream(); setRunning(false); toast("Run stopped — compute released."); break;
    case "chat_error":
      if (state.run) state.run.text = `**Chat error:** ${msg.message || ""}`;
      finishAssistantStream(); setRunning(false); toast("Chat error"); break;
    case "artifacts": appendArtifacts(msg.items || [], msg.bundle_url); break;
    case "run_complete":
      if (msg.run_id) { state.lastRunId = msg.run_id; try { localStorage.setItem(LASTRUN_KEY, msg.run_id); } catch {} }
      break;
    case "duo_prompt": showDuoPanel(msg.prompt); break;
    case "duo_done": hideDuoPanel(); break;
    case "plan_prompt": showPlanPanel(msg.agenda); break;
    case "plan_clarify": showClarify(msg.questions); break;
    case "decision_prompt": showDecision(msg.goal, msg.options); break;
    case "plan_done": hidePlanPanel(); break;
    case "error": toast(msg.message); break;
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
    campus_network_confirmed: $("campusCheck").checked,
    mock: $("mockCheck").checked,
  };
  if (!body.mock && !body.ucinetid) { toast("Enter your UCInetID."); return; }
  if (useKey && !body.credential_id && !body.mock) { toast("No saved SSH key — log in with password + Duo once first."); return; }
  const rh = $("reattachHint"); if (rh) rh.classList.add("hidden");  // hint served its purpose
  resetPipeline();
  const ls = $("logStream"); if (ls) ls.innerHTML = "";
  $("connectBtn").disabled = true;
  try {
    const res = await fetch("/api/connect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    const data = await res.json();
    if (!res.ok) { toast(data.error || "Connect failed"); $("connectBtn").disabled = false; return; }
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

async function openStorage() {
  if (!state.connectionId) return;
  $("storageModal").classList.remove("hidden");
  $("storageItems").innerHTML = '<div class="storage-loading">Loading…</div>';
  $("storageQuota").textContent = "";
  $("storageBase").textContent = "";
  $("storageUsed").textContent = "—";
  try {
    const res = await fetch(`/api/storage/${state.connectionId}`);
    const d = await res.json();
    if (!res.ok) { $("storageItems").innerHTML = `<div class="storage-empty">${escapeHtml(d.error || "Could not read storage")}</div>`; return; }
    renderStorage(d);
  } catch (err) {
    $("storageItems").innerHTML = `<div class="storage-empty">${isUnreachable(err) ? "Can't reach the local server." : escapeHtml(err.message)}</div>`;
  }
}

function renderStorage(d) {
  $("storageBase").textContent = d.base || "";
  $("storageUsed").textContent = d.used || "?";
  $("storageQuota").textContent = d.quota_raw || "";
  const items = d.items || [];
  if (!items.length) { $("storageItems").innerHTML = '<div class="storage-empty">No items in your directory.</div>'; return; }
  $("storageItems").innerHTML = items.map((i) =>
    `<div class="storage-item"><span class="si-name">${escapeHtml(i.name)}</span><span class="si-size">${escapeHtml(i.size)}</span><button class="si-del" data-path="${escapeHtml(i.path)}">Delete</button></div>`
  ).join("");
}

async function deleteStorageItem(path) {
  if (!confirm(`Delete on HPC3 (rm -rf):\n\n${path}\n\nThis cannot be undone. Continue?`)) return;
  try {
    const res = await fetch("/api/storage/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connection_id: state.connectionId, path }) });
    const d = await res.json();
    if (!res.ok) { toast(d.error || "Delete failed"); return; }
    toast("Deleted.");
    renderStorage(d);
  } catch (err) { toast(isUnreachable(err) ? "Can't reach the local server." : "Delete failed: " + err.message); }
}

async function stopGpu() {
  if (!state.connectionId) return;
  if (!confirm("Stop YOUR OWN Qwen3 GPU job (scancel)? Frees the GPU and stops your SU charge — only your own job, no one else is affected.")) return;
  await doStopGpu();
}

async function disconnect() {
  if (!state.connectionId) { applyStatus({ status: "disconnected", model: "" }); return; }
  if (state.lastJobId && state.status === "ready") {
    if (confirm(`GPU job ${state.lastJobId} is still running and keeps charging SU.\n\nOK = stop it too (scancel, frees your SU)\nCancel = just disconnect, leave it running`)) {
      await doStopGpu();
    }
  }
  try {
    await fetch("/api/disconnect", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connection_id: state.connectionId }) });
  } catch (err) {
    if (isUnreachable(err)) toast("Local server was already down — clearing the session. Check HPC3 with `squeue --me`.");
  }
  if (state.ws) { try { state.ws.close(); } catch {} }
  try { localStorage.removeItem(CONN_KEY); } catch {}
  state.connectionId = null;
  state.lastJobId = null;
  applyStatus({ status: "disconnected", model: "" });
}

function setRunning(running) {
  state.running = running;
  // Any legit terminal event (chat_done / chat_error / run_complete) clears `running` and so cancels
  // the silent-death watchdog — only a run that ended with NO terminal event lets the timer fire.
  if (!running && state.deadRunTimer) { clearTimeout(state.deadRunTimer); state.deadRunTimer = null; }
  const stop = $("chatStop");
  if (stop) { stop.disabled = false; stop.textContent = "■ Stop"; }
  updateComposerButton();
}

// The composer has ONE primary action that swaps between Send and Stop:
//  • idle (no run, no plan)              → Send  (starts a new run)
//  • plan review / executing + has text  → Send  (refine the plan, or inject mid-run)
//  • plan review / executing + empty box → Stop  (cancel the pending plan / the run)
// So the moment the user types during a plan review OR a running analysis, Stop becomes
// Send and they can submit — without the giant Stop button hiding the input.
function updateComposerButton() {
  const send = $("chatSend"), stop = $("chatStop"), input = $("chatInput");
  if (!send || !stop || !input) return;
  const hasText = input.value.trim().length > 0;
  const busy = state.planPending || state.running;
  const showSend = !busy || hasText;             // idle → Send; busy → Send only once typing
  send.classList.toggle("hidden", !showSend);
  stop.classList.toggle("hidden", showSend);
}

// Cancel the in-flight run so the GPU + compute are released. Used by the Stop
// button and by deleting the chat that owns the run.
async function stopRun() {
  if (!state.connectionId) return;
  const stop = $("chatStop");
  if (stop) { stop.disabled = true; stop.textContent = "■ Stopping…"; }
  try {
    await fetch("/api/chat/stop", {
      method: "POST", headers: { "Content-Type": "application/json" },
      // Stop only the run THIS window launched — not whatever run is live on the shared session.
      body: JSON.stringify({ connection_id: state.connectionId, conversation_id: state.runSessionId || state.activeId }),
    });
  } catch { /* the chat_stopped event (or next render) will settle the UI */ }
}

// Steer an executing run without stopping it — the note is folded into the remaining
// steps by the lab loop (see /api/chat/inject).
async function injectPrompt(text) {
  try {
    const res = await fetch("/api/chat/inject", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, conversation_id: state.runSessionId || state.activeId, text }),
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
  // While a PI plan/clarify is awaiting a decision, the composer is the "refine the
  // plan" channel — the message is natural-language feedback that re-plans (it does NOT
  // start a new run).
  if (state.planPending) {
    appendUserMessage(text);
    input.value = "";
    updateComposerButton();
    submitPlan("revise", text);
    return;
  }
  // A run is already executing (post-plan): the message is mid-run steering — queue it for
  // the lab loop to fold into the remaining steps, rather than launching a 2nd run.
  if (state.running) {
    appendUserMessage(text);
    input.value = "";
    updateComposerButton();
    injectPrompt(text);
    return;
  }
  // A run needs a served model, and the session only has one at "ready" — mirrors the /api/lab guard.
  if (state.status !== "ready") { toast("Connect to HPC3 first."); return; }
  appendUserMessage(text);
  // Bind this run to the session that launched it, so its streamed report and
  // artifacts land here even if the user switches chats while it runs (pipelines
  // take minutes). Persist it too, so a reload/reconnect (which replays the live run)
  // re-binds the owner instead of falling back to whatever chat is active — the
  // cross-conversation leak.
  state.runSessionId = state.activeId;
  try { localStorage.setItem(RUNOWNER_KEY, JSON.stringify({ connId: state.connectionId, sid: state.activeId })); } catch {}
  input.value = "";
  setRunning(true);
  const history = activeSession().messages.filter((m) => !m.kind && (m.role === "user" || m.role === "assistant")).slice(0, -1).map((m) => ({ role: m.role, content: m.content }));
  const dataset_path = $("datasetInput").value.trim() || null;
  // The BIND-SET (feature ②): every attached data file, ordered as attached (the server re-ranks to
  // pick the primary). Legacy single-file runs still send `dataset_path`; both together stay
  // backward-compatible (the server treats `dataset_path` as the primary when `datasets` is absent).
  const datasets = state.boundPaths.map((p) => {
    const d = state.datasets.find((x) => x.path === p) || {};
    return { path: p, name: d.name || p.split("/").pop() };
  });
  const autonomous = !!($("autonomousMode") && $("autonomousMode").checked);
  // Bypass mode overrides plan review — don't also send plan_mode=true (avoids a stale plan card).
  const plan_mode = !autonomous && !!($("planMode") && $("planMode").checked);
  // DAG is the default execution model now (dependency-graph + Coordinator; no UI toggle). The
  // classic linear order is still reachable server-side via BIOAGENT_PLANNER=linear.
  const planner = "dag";
  try {
    // The research lab (PI→Scientist→Critic + real scanpy/gseapy tools) is the one execution path.
    // Manual (default): plan review + DAG decision-point pauses. Bypass (autonomous): no gates.
    const sess = activeSession();
    // Axis B. `route: "chat"` takes the answer-first fast path (agents/quick_chat.py) — no plan, no
    // report; `history` gives that loop the conversation it is continuing (the research path ignores
    // it and scopes a study by question + dataset instead). An older client omitting `route` still
    // gets today's research behaviour, because the server defaults it.
    const route = activeRoute();
    const res = await fetch("/api/lab", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ connection_id: state.connectionId, conversation_id: state.activeId, question: text, dataset_path, datasets, case_note: (state.caseNote && state.caseNote.text) || null, plan_mode, autonomous, planner, route, history, mode: ($("modeSelect") && $("modeSelect").value) || "auto", presets: (sess && sess.presetKeys) || [], skills: (sess && sess.skillKeys) || [], preset_prompt: null }) });
    if (!res.ok) { const d = await res.json(); toast(d.error || "Run failed"); }
  } catch (err) { toast(isUnreachable(err) ? "Can't reach the local server." : "Run failed: " + err.message); }
}

// ---- plan-mode review: the plan/clarify render AS chat messages (plan.md style) -------
// The PI proposes a plan rendered into the conversation like a plan.md, with inline
// Run / Cancel actions. The user refines it by typing in the composer (which re-plans);
// an ambiguous request comes back as a clarify card with option chips. The live card is
// ephemeral (buttons can't persist); on approval the plan is saved to history as text.
const DEFAULT_PLACEHOLDER = "Ask AiScientist to plan or interpret an ocular bioinformatics analysis...";

function removePlanCard() {
  if (state.planCardEl) { state.planCardEl.remove(); state.planCardEl = null; }
}

function planCardShell() {
  removePlanCard();
  const stream = $("chatStream");
  const hint = stream.querySelector(".empty-hint"); if (hint) hint.remove();
  const el = document.createElement("div");
  el.className = "msg assistant plan-card";
  stream.appendChild(el);
  state.planCardEl = el;
  state.planPending = true;
  return el;
}

function showPlanPanel(agenda) {
  const el = planCardShell();
  state.planAgenda = agenda || [];
  const md = "## 📋 Proposed plan\n\n" + state.planAgenda.map((s, i) => `${i + 1}. ${s}`).join("\n");
  el.innerHTML = renderMarkdown(md) +
    `<div class="plan-actions-row">
       <button class="primary small" data-plan="approve">▶ Run this plan</button>
       <button class="ghost small" data-plan="cancel">Cancel</button>
       <span class="plan-actions-hint">…or type changes below to refine it</span>
     </div>`;
  el.querySelector('[data-plan="approve"]').addEventListener("click", () => submitPlan("approve"));
  el.querySelector('[data-plan="cancel"]').addEventListener("click", () => submitPlan("cancel"));
  $("chatInput").placeholder = "Tell the PI what to change (e.g. 'drop the enrichment step')…";
  $("chatStream").scrollTop = $("chatStream").scrollHeight;
  updateComposerButton();
}

function showClarify(questions) {
  const el = planCardShell();
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
      chip.addEventListener("click", () => submitPlan("revise", q.question + ": " + opt));
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
  const el = planCardShell();
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
    chip.addEventListener("click", () => submitDecision(opt));
    opts.appendChild(chip);
  }
  // Always offer to defer to the agent, even when the plan gave no explicit options.
  const defer = document.createElement("button");
  defer.type = "button";
  defer.className = "clarify-chip ghost";
  defer.textContent = "Let the agent decide";
  defer.addEventListener("click", () => submitDecision(""));
  opts.appendChild(defer);
  el.appendChild(opts);
  $("chatInput").placeholder = "Pick an option — or leave it to the agent…";
  $("chatStream").scrollTop = $("chatStream").scrollHeight;
  updateComposerButton();
}

// choice "" = proceed with the agent's judgment; a non-empty string = the chosen approach. Sent as a
// "revise" (the backend's decision_review treats any non-cancel action as proceed, choice=feedback).
async function submitDecision(choice) {
  if (!state.connectionId) return;
  finalizePlanCard(choice ? `✓ Chose: ${choice}` : "✓ Proceeding — agent decides");
  state.planPending = false;
  $("chatInput").placeholder = DEFAULT_PLACEHOLDER;
  mountStream();
  repaintWorking();
  updateComposerButton();
  try {
    await fetch("/api/lab/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, conversation_id: state.runSessionId || state.activeId, action: "revise", feedback: choice || "" }),
    });
    toast(choice ? "Decision recorded — continuing…" : "Continuing…");
  } catch (err) { toast("Failed to send decision: " + err.message); }
}

// Resolve the live plan/clarify card with a short status line (drop the buttons), and on
// approval persist the plan to history so it survives a reload.
function finalizePlanCard(note, persistMarkdown) {
  if (state.planCardEl) {
    const row = state.planCardEl.querySelector(".plan-actions-row");
    if (row) row.innerHTML = `<span class="plan-actions-status">${escapeHtml(note)}</span>`;
    state.planCardEl = null;
  }
  if (persistMarkdown) pushToSession(state.runSessionId || state.activeId, { role: "assistant", content: persistMarkdown });
}

function hidePlanPanel() {
  // plan_done (any resolution). Card finalization happens in submitPlan; here we only
  // clear the pending state + restore the composer (idempotent).
  state.planPending = false;
  $("chatInput").placeholder = DEFAULT_PLACEHOLDER;
  updateComposerButton();
}

// action: "approve" | "revise" | "cancel"; feedback only for "revise".
async function submitPlan(action, feedback) {
  if (!state.connectionId) return;
  const agendaMd = (state.planAgenda && state.planAgenda.length)
    ? "## 📋 Research plan\n\n" + state.planAgenda.map((s, i) => `${i + 1}. ${s}`).join("\n")
    : null;
  if (action === "approve") {
    finalizePlanCard("▶ Approved — running…", agendaMd);
    // Re-anchor the live bubble below the just-approved plan so the run's activity
    // (reasoning / running code / report) streams in AFTER it, where the user is looking.
    mountStream();
    repaintWorking();
  }
  else if (action === "cancel") finalizePlanCard("✕ Cancelled");
  else finalizePlanCard("✎ Sent changes to the PI — re-planning…");
  state.planPending = false;
  $("chatInput").placeholder = DEFAULT_PLACEHOLDER;
  updateComposerButton();
  try {
    await fetch("/api/lab/plan", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, conversation_id: state.runSessionId || state.activeId, action, feedback: feedback || "" }),
    });
    if (action === "approve") toast("Plan approved — running…");
    else if (action === "cancel") toast("Plan cancelled.");
    else toast("Sent to the PI — re-planning…");
  } catch (err) { toast(isUnreachable(err) ? "Can't reach the local server." : "Plan submit failed: " + err.message); }
}

// Upload a local dataset file to the server (saved under the connection's per-user
// workspace); on success fill the dataset path so the lab/pipeline can analyze it.
// Small files go in one POST; large files (> threshold) upload in resumable chunks
// so a dropped connection resumes from the server's byte offset instead of
// restarting. Big matrices are still better pointed at via a server path.
const UPLOAD_CHUNK = 8 * 1024 * 1024;               // 8 MB per chunk
const UPLOAD_CHUNK_THRESHOLD = 16 * 1024 * 1024;    // above this → resumable chunks

async function uploadDataset(file) {
  if (!file) return;
  if (!state.connectionId) { toast("Connect to HPC3 first."); return; }
  // Stable per-file id (same formula as uploadResumable) so a Resume reuses the same record + the
  // server's byte offset. The Datasets tab renders this record live so the transfer is transparent.
  const id = "u" + Math.abs(hashStr(`${file.name}:${file.size}:${file.lastModified}`)).toString(36);
  const ctrl = new AbortController();   // lets Cancel abort the in-flight chunk + stop the loop
  upsertUpload({ id, name: file.name, size: file.size, sent: 0, status: "uploading", file, error: "", ctrl });
  setUploadProgress(`Uploading ${file.name}…`);
  try {
    const onProg = (pct, phase) => {
      upsertUpload({ id, sent: Math.round((pct / 100) * file.size), status: phase === "finalizing" ? "finalizing" : "uploading" });
      setUploadProgress(phase === "finalizing" ? `Finalizing ${file.name}…` : `Uploading ${file.name}… ${pct}%`);
    };
    const d = file.size > UPLOAD_CHUNK_THRESHOLD
      ? await uploadResumable(file, onProg, ctrl.signal, id)
      : await uploadSingle(file, ctrl.signal);
    if (!d) { upsertUpload({ id, status: "error", error: "upload failed" }); return; }
    upsertUpload({ id, sent: file.size, status: "done" });
    // Carry the upload-time content skim (feature ①'s deterministic peek gist) onto the chip so the
    // user sees "what this is" per attached file, not just the name.
    addDataset({ name: d.name, path: d.path, kind: "file", gist: (d.peek && d.peek.gist) || "" });
    toast(`Uploaded ${d.name} (${(d.size / 1e6).toFixed(1)} MB)`);   // d.name may be uniquified
    loadDatasets();                                                  // reflect the new file in the tab
    setTimeout(() => removeUpload(id), 4000);                        // clear the "Done" card after a beat
  } catch (err) {
    if (ctrl.signal.aborted || err.name === "AbortError") {          // user cancelled — not an error
      upsertUpload({ id, status: "cancelled" });
      discardUpload(id);                                             // drop the server-side .part too
      toast(`Cancelled ${file.name}`);
      setTimeout(() => removeUpload(id), 4000);
      return;
    }
    upsertUpload({ id, status: "error", error: err.message || "upload failed" });
    toast(isUnreachable(err) ? "Can't reach the local server." : "Upload failed: " + err.message);
  } finally { setUploadProgress(null); }
}

// --- Datasets-tab upload tracker: live progress + resume + cancel for chunked uploads ---------
function upsertUpload(rec) {
  const i = state.uploads.findIndex((u) => u.id === rec.id);
  if (i >= 0) state.uploads[i] = { ...state.uploads[i], ...rec };
  else state.uploads.unshift(rec);
  renderUploads();
}
function removeUpload(id) { state.uploads = state.uploads.filter((u) => u.id !== id); renderUploads(); }
function resumeUpload(id) { const u = state.uploads.find((x) => x.id === id); if (u && u.file) uploadDataset(u.file); }
// Cancel: abort the in-flight fetch + stop the chunk loop; uploadDataset's catch finalises the card.
function cancelUpload(id) { const u = state.uploads.find((x) => x.id === id); if (u && u.ctrl) u.ctrl.abort(); }
function cancelActiveUploads() {
  state.uploads.filter((u) => u.status === "uploading" || u.status === "finalizing").forEach((u) => cancelUpload(u.id));
}
// Tell the server to delete the half-written .part so a cancelled upload leaves nothing behind.
function discardUpload(id) {
  fetch("/api/upload/discard", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connection_id: state.connectionId, upload_id: id }) }).catch(() => {});
}

function renderUploads() {
  const host = $("uploadsPanel");
  if (!host) return;
  if (!state.uploads.length) { host.innerHTML = ""; host.classList.add("hidden"); return; }
  host.classList.remove("hidden");
  host.innerHTML = state.uploads.map((u) => {
    const pct = u.size ? Math.min(100, Math.round((u.sent / u.size) * 100)) : 0;
    const ic = u.status === "error" ? "error" : u.status === "cancelled" ? "cancel"
      : u.status === "done" ? "check_circle" : "upload_file";
    const stat = u.status === "done" ? "Done"
      : u.status === "cancelled" ? "Cancelled"
      : u.status === "finalizing" ? "Finalizing on HPC3…"
      : u.status === "error" ? `Failed — ${u.error || "interrupted"}`
      : `${pct}% · ${(u.sent / 1e6).toFixed(1)} / ${(u.size / 1e6).toFixed(1)} MB`;
    const active = u.status === "uploading" || u.status === "finalizing";
    const btn = active ? `<button type="button" class="ghost small" data-cancel="${u.id}">Cancel</button>`
      : (u.status === "error" && u.file ? `<button type="button" class="ghost small" data-resume="${u.id}">Resume</button>` : "");
    return `<div class="upload-card ${u.status}"><div class="upload-row">` +
      `<span class="material-symbols-outlined msym">${ic}</span>` +
      `<span class="upload-name" title="${escapeHtml(u.name)}">${escapeHtml(u.name)}</span>` +
      `<span class="upload-stat">${escapeHtml(stat)}</span>${btn}</div>` +
      `<div class="meter"><div class="meter-fill${u.status === "done" ? " mem" : ""}" style="width:${pct}%"></div></div></div>`;
  }).join("");
}

// Upload a WHOLE folder (webkitdirectory) — every file keeps its nested relative path, so
// a multi-level folder round-trips intact. Registered as ONE dataset entry; all uploads
// this session stay reachable to the agent (server exposes the uploads tree to run_code).
async function uploadFolder(fileList) {
  const files = Array.from(fileList || []).filter((f) => f && f.size >= 0);
  if (!files.length) return;
  if (!state.connectionId) { toast("Connect to HPC3 first."); return; }
  const folder0 = (files[0].webkitRelativePath || files[0].name).split("/")[0];
  setUploadProgress(`Preparing ${folder0}/…`);
  try {
    // Reserve a NON-colliding top-level folder name first, so a second upload of the same
    // folder lands in "name (1)/" instead of merging into / overwriting the first.
    let folder = folder0;
    try {
      const rr = await fetch("/api/upload/reserve-folder", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ connection_id: state.connectionId, name: folder0 }),
      });
      const rd = await rr.json().catch(() => ({}));
      if (rr.ok && rd.folder) folder = rd.folder;
    } catch { /* fall back to the raw name */ }
    let done = 0;
    for (const f of files) {
      const rel0 = f.webkitRelativePath || `${folder0}/${f.name}`;
      const parts = rel0.split("/"); parts[0] = folder; const rel = parts.join("/");
      const fd = new FormData();
      fd.append("connection_id", state.connectionId);
      fd.append("rel_path", rel);
      fd.append("file", f);
      const res = await fetch("/api/upload", { method: "POST", body: fd });
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(e.error || `upload failed (${res.status})`); }
      done += 1;
      setUploadProgress(`Uploading ${folder}/ — ${done}/${files.length}`);
    }
    const reg = await fetch("/api/upload/register-folder", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connection_id: state.connectionId, folder }),
    });
    const d = await reg.json().catch(() => ({}));
    if (!reg.ok) throw new Error(d.error || "folder registration failed");
    addDataset({ name: d.name + "/", path: d.path, kind: "folder" });
    toast(`Uploaded folder ${d.name}/ (${d.count} files, ${(d.size / 1e6).toFixed(1)} MB)`);
  } catch (err) {
    toast(isUnreachable(err) ? "Can't reach the local server." : "Folder upload failed: " + err.message);
  } finally { setUploadProgress(null); }
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
// The primary = highest-ranked bound file (attach order breaks ties) — mirrors the server.
function primaryBoundPath() {
  if (!state.boundPaths.length) return null;
  const meta = state.boundPaths.map((p, i) => {
    const d = state.datasets.find((x) => x.path === p);
    return { p, i, r: _suffixRank(d ? d.name : p) };
  });
  meta.sort((a, b) => a.r - b.r || a.i - b.i);
  return meta[0].p;
}

function addDataset(d) {
  if (!d || !d.path) return;
  if (!state.datasets.some((x) => x.path === d.path)) state.datasets.unshift(d);
  if (!state.boundPaths.includes(d.path)) state.boundPaths.push(d.path);   // ADD to the set
  syncDatasetSelection();
}

// Add/remove one path from the bind-set (a recent-list toggle or a chip ×).
function toggleBound(path) {
  if (!path) return;
  const i = state.boundPaths.indexOf(path);
  if (i >= 0) state.boundPaths.splice(i, 1); else state.boundPaths.push(path);
  syncDatasetSelection();
}

function clearBound() { state.boundPaths = []; syncDatasetSelection(); }

function syncDatasetSelection() {
  state.datasetPath = primaryBoundPath();
  $("datasetInput").value = state.datasetPath || "";   // legacy primary, for back-compat readers
  // Not persisted across reloads on purpose: a new chat should start with an empty bind-set
  // rather than silently re-attaching the last one (see loadDatasetChips).
  renderDatasetChips();
  renderDataMenuRecent();
}
// Back-compat alias: some call sites still say selectDataset(path|null) — a single-file select is just
// "replace the set with this one" (or clear it).
function selectDataset(path) { state.boundPaths = path ? [path] : []; syncDatasetSelection(); }

function renderDatasetChips() {
  const host = $("datasetChips");
  if (!host) return;
  if (!state.boundPaths.length) { host.innerHTML = '<span class="dataset-empty">No dataset selected</span>'; return; }
  const primary = primaryBoundPath();
  host.innerHTML = state.boundPaths.map((path) => {
    const d = state.datasets.find((x) => x.path === path) || { name: path.split("/").pop(), path, kind: "file" };
    const ico = d.kind === "folder" ? "folder" : "draft";
    const isPrimary = path === primary && state.boundPaths.length > 1;
    const gist = d.gist ? ` — ${d.gist}` : "";                 // the file's ① skim, when known
    return `<span class="dataset-chip active" title="${escapeHtml(d.path + gist)}">` +
      `<span class="material-symbols-outlined msym">${ico}</span>` +
      `<span class="dataset-chip-name">${escapeHtml(d.name)}${isPrimary ? " ★" : ""}</span>` +
      `<button type="button" class="dataset-chip-x" data-clear-path="${escapeHtml(path)}" title="Remove this file">×</button></span>`;
  }).join("");
}

// ---- the case note: the SECOND attachment (clinical description → HPO) ---------------------
// Read client-side and sent as TEXT with the run, so it never competes for the single dataset slot.
const MAX_CASE_NOTE_CHARS = 64000;   // mirrors _MAX_CASE_NOTE_CHARS in the gateway

async function attachCaseNote(file) {
  if (!file) return;
  try {
    const text = (await file.text()).trim();
    if (!text) { toast("That note is empty."); return; }
    if (text.length > MAX_CASE_NOTE_CHARS) toast(`Note is long — only the first ${MAX_CASE_NOTE_CHARS / 1000}k characters will be used.`);
    state.caseNote = { name: file.name, text: text.slice(0, MAX_CASE_NOTE_CHARS) };
    renderCaseNoteChip();
  } catch (err) {
    toast("Couldn't read that note: " + err.message);
  }
}

function renderCaseNoteChip() {
  const host = $("caseNoteChips");
  if (!host) return;
  if (!state.caseNote) { host.innerHTML = ""; return; }   // absent by default — no empty-state noise
  const n = state.caseNote;
  const preview = n.text.slice(0, 160) + (n.text.length > 160 ? "…" : "");
  host.innerHTML = `<span class="dataset-chip active" title="${escapeHtml(preview)}">` +
    `<span class="material-symbols-outlined msym">clinical_notes</span>` +
    `<span class="dataset-chip-name">${escapeHtml(n.name)} (${n.text.length} chars)</span>` +
    `<button type="button" class="dataset-chip-x" data-clear-note title="Remove the case note">×</button></span>`;
}

// The popover's "Recent uploads" list — all known uploads (this session + server history).
function renderDataMenuRecent() {
  const host = $("dataMenuRecent");
  if (!host) return;
  if (!state.datasets.length) { host.innerHTML = '<div class="data-menu-empty">No uploads yet</div>'; return; }
  host.innerHTML = state.datasets.map((d) => {
    const active = state.boundPaths.includes(d.path) ? " active" : "";   // in the bind-set → checked
    const ico = d.kind === "folder" ? "folder" : "draft";
    return `<button type="button" class="data-menu-recent-item${active}" data-path="${escapeHtml(d.path)}" title="${escapeHtml(d.path)} (click to add/remove)">` +
      `<span class="material-symbols-outlined msym">${ico}</span><span class="dmr-name">${escapeHtml(d.name)}</span>` +
      (active ? `<span class="material-symbols-outlined msym dmr-check">check</span>` : "") + `</button>`;
  }).join("");
}

function setUploadProgress(text) {
  const el = $("uploadStatus"), cb = $("uploadCancel");
  if (!el) return;
  if (!text) { el.classList.add("hidden"); el.textContent = ""; if (cb) cb.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.textContent = text;
  // Show the composer's Cancel button while any upload is actually in flight.
  if (cb) cb.classList.toggle("hidden", !state.uploads.some((u) => u.status === "uploading" || u.status === "finalizing"));
}

function toggleDataMenu(force) {
  const m = $("dataMenu");
  if (!m) return;
  const show = force !== undefined ? force : m.classList.contains("hidden");
  m.classList.toggle("hidden", !show);
  if (show) renderDataMenuRecent();
}

// Populate the recent list from this user's server-side dataset history so earlier uploads
// (incl. folders from a previous visit) are selectable — not just ones uploaded this session.
async function loadDatasetChips() {
  try {
    const items = (await (await fetch("/api/datasets")).json()).datasets || [];
    for (const x of items) {
      const name = x.kind === "folder" ? x.name + "/" : x.name;
      if (!state.datasets.some((d) => d.path === x.path)) state.datasets.push({ name, path: x.path, kind: x.kind });
    }
  } catch { /* datasets need accounts; the list just stays empty otherwise */ }
  // Do NOT auto-select the last dataset: a fresh chat starts with an EMPTY dataset box rather than
  // silently inheriting whatever was loaded last time (that surprised users and mis-attached data to
  // a new question). Prior uploads still appear as clickable chips; the user picks one explicitly.
  renderDatasetChips();
  renderDataMenuRecent();
}

async function uploadSingle(file, signal) {
  const fd = new FormData();
  fd.append("connection_id", state.connectionId);
  fd.append("file", file);
  const res = await fetch("/api/upload", { method: "POST", body: fd, signal });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) { toast(d.error || "Upload failed"); return null; }
  return d;
}

// Resumable chunked upload: ask the server how much it already has, then send the
// rest in chunks (each retried with backoff). The upload id is stable per
// (name + size + mtime) so retrying the SAME file resumes; a different file is fresh.
async function uploadResumable(file, onProgress, signal, uploadId) {
  uploadId = uploadId || ("u" + Math.abs(hashStr(`${file.name}:${file.size}:${file.lastModified}`)).toString(36));
  const abort = () => { if (signal && signal.aborted) throw new DOMException("cancelled", "AbortError"); };
  let received = 0;
  try {
    const st = await api("GET", `/api/upload/status?connection_id=${encodeURIComponent(state.connectionId)}&upload_id=${encodeURIComponent(uploadId)}`);
    received = Math.min(st.received || 0, file.size);
  } catch { received = 0; }
  let offset = received;
  while (offset < file.size) {
    abort();
    const end = Math.min(offset + UPLOAD_CHUNK, file.size);
    const isLast = end >= file.size;
    // The `done` chunk also stages the whole file to HPC3 server-side (can take a while for a big
    // matrix) — surface that as a "finalizing" phase so a slow last request never looks stuck.
    if (isLast) onProgress && onProgress(100, "finalizing");
    const d = await postChunkWithRetry(uploadChunkForm(file, uploadId, offset, end, isLast), 6, signal);
    if (d.status === "uploaded") { onProgress && onProgress(100); return d; }
    offset = typeof d.received === "number" ? d.received : end;   // server is the source of truth
    onProgress && onProgress(Math.floor((offset / file.size) * 100));
  }
  // All bytes are on the server but it was never finalized (resumed exactly at EOF, e.g. after a
  // finalize that timed out): send a final done-chunk to finalize/re-stage.
  abort();
  onProgress && onProgress(100, "finalizing");
  return await postChunkWithRetry(uploadChunkForm(file, uploadId, file.size, file.size, true), 6, signal);
}

function uploadChunkForm(file, uploadId, start, end, done) {
  const fd = new FormData();
  fd.append("connection_id", state.connectionId);
  fd.append("upload_id", uploadId);
  fd.append("name", file.name);
  fd.append("offset", String(start));
  fd.append("done", done ? "true" : "false");
  fd.append("chunk", file.slice(start, end), "chunk");
  return fd;
}

async function postChunkWithRetry(fd, tries = 6, signal) {
  let lastErr;
  for (let i = 0; i < tries; i++) {
    if (signal && signal.aborted) throw new DOMException("cancelled", "AbortError");
    try {
      const res = await fetch("/api/upload/chunk", { method: "POST", body: fd, signal });
      const d = await res.json().catch(() => ({}));
      if (res.ok) return d;
      if (res.status === 409 && typeof d.received === "number") return d;   // resync to true offset
      lastErr = new Error(d.error || `chunk failed (${res.status})`);
    } catch (e) { if (e.name === "AbortError") throw e; lastErr = e; }       // cancel → stop, don't retry
    await new Promise((r) => setTimeout(r, Math.min(5000, 500 * 2 ** i)));  // exp backoff (capped 5s)
  }
  throw lastErr || new Error("chunk upload failed");
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

function setPanel(side, open) {
  $("layout").classList.toggle(`collapse-${side}`, !open);
  $(side === "left" ? "toggleLeft" : "toggleRight").classList.toggle("active", open);
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
  if (!_appInited) { init(); initShell(); _appInited = true; }
  renderAccount(user);
  switchView("research");
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
  location.reload();
}

function initShell() {
  document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
  $("datasetsRefresh").addEventListener("click", loadDatasets);
  $("runsRefresh").addEventListener("click", loadRuns);
  $("adminRefresh").addEventListener("click", loadAdminUsers);
  $("systemRefresh").addEventListener("click", loadSystem);
  $("createUserForm").addEventListener("submit", createUser);
  $("changePwForm").addEventListener("submit", changePassword);
  $("logoutBtn").addEventListener("click", doLogout);
  $("userTableBody").addEventListener("click", onUserTableClick);
  let _userSearchTimer = null;
  $("userSearch").addEventListener("input", () => { clearTimeout(_userSearchTimer); _userSearchTimer = setTimeout(loadAdminUsers, 250); });
  $("userSearch").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); clearTimeout(_userSearchTimer); loadAdminUsers(); } });
  $("userSearchClear").addEventListener("click", () => { $("userSearch").value = ""; loadAdminUsers(); });
  $("datasetsList").addEventListener("click", onDatasetsClick);
  $("uploadsPanel").addEventListener("click", (e) => {
    const r = e.target.closest("[data-resume]"); if (r) { resumeUpload(r.dataset.resume); return; }
    const c = e.target.closest("[data-cancel]"); if (c) { cancelUpload(c.dataset.cancel); }
  });
  const uc = $("uploadCancel");
  if (uc) uc.addEventListener("click", cancelActiveUploads);   // composer-side Cancel
}

function switchView(name) {
  document.querySelectorAll(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + name));
  if (name === "datasets") loadDatasets();
  else if (name === "runs") loadRuns();
  else if (name === "admin") loadAdminUsers();
  else if (name === "account") renderAccount(state.user);
  else if (name === "system") loadSystem();
}

// ---- System view (live agents / tools / capabilities / roadmap) ----
async function loadSystem() {
  const el = $("systemBody"); el.innerHTML = '<div class="empty-hint">Loading…</div>';
  try {
    const d = await (await fetch("/api/system")).json();
    const cap = (k, v) => `<span class="cap ${v ? "on" : "off"}">${v ? "✓" : "✕"} ${escapeHtml(k)}</span>`;
    const caps = Object.entries(d.capabilities || {}).map(([k, v]) => cap(k, v)).join("");
    const agents = (d.agents || []).map((a) =>
      `<div class="card"><div class="card-main"><strong>${escapeHtml(a.name)}</strong>` +
      `<span class="muted">${escapeHtml(a.role)} · ${escapeHtml(a.where)}</span>` +
      `<span class="sys-desc">${escapeHtml(a.description)}</span></div></div>`).join("");
    // tools grouped by category
    const byCat = {};
    (d.tools || []).forEach((t) => { (byCat[t.category] = byCat[t.category] || []).push(t); });
    const tools = Object.keys(byCat).sort().map((c) =>
      `<div class="sys-cat"><h4>${escapeHtml(c)}</h4>` + byCat[c].map((t) =>
        `<div class="card"><div class="card-main"><strong>${escapeHtml(t.name)}` +
        `${t.reads_private_data ? ' <span class="tag-priv">private data</span>' : ""}</strong>` +
        `<span class="sys-desc">${escapeHtml(t.description)}</span>` +
        `${t.requires.length ? `<span class="muted">needs: ${t.requires.map(escapeHtml).join(", ")}</span>` : ""}</div>` +
        `<span class="cap ${t.available ? "on" : "off"}">${t.available ? "available" : "unavailable"}</span></div>`
      ).join("") + "</div>").join("");
    const flows = (d.workflows || []).map((w) =>
      `<div class="card flow"><div class="card-main"><strong>${escapeHtml(w.name)} ` +
      `<span class="flow-kind">${escapeHtml(w.kind)}</span></strong>` +
      `<span class="sys-desc">${escapeHtml(w.description)}</span>` +
      `<div class="flow-stages">${(w.stages || []).map((s) =>
        `<span class="flow-stage">${escapeHtml(s)}</span>`).join('<span class="flow-arrow">→</span>')}</div>` +
      `</div></div>`).join("");
    const specialists = (d.specialists || []).map((s) =>
      `<div class="card"><div class="card-main"><strong>${escapeHtml(s.name)}</strong>` +
      `<span class="sys-desc">${escapeHtml(s.persona)}</span></div></div>`).join("");
    el.innerHTML =
      `<h3>Capabilities (this server)</h3><div class="cap-row">${caps}</div>` +
      `<h3>Agents</h3><div class="card-list">${agents}</div>` +
      (specialists ? `<h3>Scientist specialists</h3><div class="card-list">${specialists}</div>` : "") +
      `<h3>Workflow presets</h3><div class="card-list">${flows}</div>` +
      `<h3>Tools / skills</h3>${tools}`;
    renderWorkflowGraph(d.graph);
  } catch { el.innerHTML = '<div class="empty-hint">Failed to load system overview.</div>'; }
}

// ---- Live workflow graph (read-only; reflects the current code) ----
let _cy = null;
const _GRAPH_FILL = { agent: "#DCCCFF", output: "#C6FAF6", preset: "#FFE0C2", persona: "#FFECBD", tool: "#CDF4D3" };
const _GRAPH_STROKE = { agent: "#874FFF", output: "#5AD8CC", preset: "#FF9E42", persona: "#FFC943", tool: "#66D575" };

function renderWorkflowGraph(graph) {
  const host = $("systemGraph");
  if (!host) return;
  if (typeof cytoscape === "undefined") { host.innerHTML = '<div class="empty-hint">Graph library not loaded.</div>'; return; }
  if (!graph || !(graph.nodes || []).length) { host.innerHTML = '<div class="empty-hint">No workflow graph.</div>'; return; }
  if (_cy) { try { _cy.destroy(); } catch {} _cy = null; }
  host.innerHTML = "";
  const elements = [];
  graph.nodes.forEach((n) => elements.push({ data: {
    id: n.id, label: n.label, ntype: n.type, meta: n.meta || {},
    dim: n.type === "tool" && n.meta && n.meta.available === false,
  } }));
  graph.edges.forEach((e, i) => elements.push({ data: {
    id: "e" + i, source: e.source, target: e.target, label: e.label || "", etype: e.type || "flow",
  } }));
  const roots = graph.nodes.filter((n) => n.type === "preset").map((n) => n.id);
  _cy = cytoscape({
    container: host,
    elements,
    wheelSensitivity: 0.2,
    style: [
      { selector: "node", style: {
        label: "data(label)", "font-size": "10px", "text-wrap": "wrap", "text-max-width": "120px",
        "text-valign": "center", "text-halign": "center", shape: "round-rectangle",
        width: "label", height: "label", padding: "9px", color: "#1E1E1E",
        "background-color": (n) => _GRAPH_FILL[n.data("ntype")] || "#C2E5FF",
        "border-color": (n) => _GRAPH_STROKE[n.data("ntype")] || "#3DADFF", "border-width": 1.4,
      } },
      { selector: "node[?dim]", style: { opacity: 0.45, "border-style": "dashed" } },
      { selector: "node:selected", style: { "border-width": 3, "border-color": "#1E1E1E" } },
      { selector: "edge", style: {
        label: "data(label)", "font-size": "8px", color: "#8a929c", "curve-style": "bezier",
        "target-arrow-shape": "triangle", width: 1.2, "line-color": "#b8bfc8", "target-arrow-color": "#b8bfc8",
        "text-background-color": "#ffffff", "text-background-opacity": 1, "text-background-padding": "1px",
      } },
      { selector: 'edge[etype="steer"]', style: { "line-style": "dashed", "line-color": "#FF9E42", "target-arrow-color": "#FF9E42" } },
      { selector: 'edge[etype="persona"]', style: { "line-style": "dashed", "line-color": "#FFC943", "target-arrow-color": "#FFC943" } },
      { selector: 'edge[etype="tool"]', style: { "line-style": "dashed", "line-color": "#66D575", "target-arrow-color": "#66D575" } },
      { selector: 'edge[etype="pipeline"]', style: { "line-color": "#3DADFF", "target-arrow-color": "#3DADFF", width: 2 } },
    ],
    layout: { name: "breadthfirst", directed: true, roots: roots.length ? roots : undefined, spacingFactor: 1.15, padding: 14 },
  });
  _cy.on("tap", "node", (ev) => showGraphNodeInfo(ev.target.data()));
  _cy.fit(undefined, 24);
}

function showGraphNodeInfo(d) {
  const box = $("systemGraphInfo");
  if (!box) return;
  const m = d.meta || {};
  const rows = [`<div class="gi-title">${escapeHtml(d.label)} <span class="gi-type">${escapeHtml(d.ntype)}</span></div>`];
  if (m.category) rows.push(`<div><span class="gi-k">category</span> ${escapeHtml(m.category)}</div>`);
  if (m.where) rows.push(`<div><span class="gi-k">where</span> <code>${escapeHtml(m.where)}</code></div>`);
  if (typeof m.available === "boolean") rows.push(`<div><span class="gi-k">status</span> <span class="cap ${m.available ? "on" : "off"}">${m.available ? "available" : "unavailable"}</span></div>`);
  if (m.reads_private_data) rows.push('<div><span class="tag-priv">reads private data</span></div>');
  if (m.requires && m.requires.length) rows.push(`<div><span class="gi-k">needs</span> ${m.requires.map(escapeHtml).join(", ")}</div>`);
  if (m.description) rows.push(`<div class="gi-desc">${escapeHtml(m.description)}</div>`);
  box.innerHTML = rows.join("");
}

// ---- Datasets view ----
async function loadDatasets() {
  renderUploads();   // show any in-flight/failed uploads at the top of the tab
  const el = $("datasetsList"); el.innerHTML = '<div class="empty-hint">Loading…</div>';
  try {
    const items = (await (await fetch("/api/datasets")).json()).datasets || [];
    el.innerHTML = items.length ? items.map((x) =>
      `<div class="card"><div class="card-main"><strong>${icon(x.kind === "folder" ? "folder" : "draft")} ${escapeHtml(x.name)}${x.kind === "folder" ? "/" : ""}</strong>` +
      `<span class="muted">${escapeHtml(x.kind)} · ${(x.size_bytes / 1e6).toFixed(1)} MB · ${fmtDate(x.uploaded_at)}</span></div>` +
      `<button class="ghost small" data-use="${escapeHtml(x.path)}" data-name="${escapeHtml(x.name)}" data-kind="${escapeHtml(x.kind)}">Use</button>` +
      `<button class="ghost small danger" data-del="${x.id}" data-name="${escapeHtml(x.name)}">Delete</button></div>`).join("")
      : '<div class="empty-hint">No datasets yet — upload one in Research.</div>';
  } catch { el.innerHTML = '<div class="empty-hint">Failed to load.</div>'; }
}
async function onDatasetsClick(e) {
  const b = e.target.closest("[data-use]");
  if (b) {
    addDataset({ name: b.dataset.name || b.dataset.use.split("/").pop(), path: b.dataset.use, kind: b.dataset.kind || "file" });
    switchView("research");
    toast("Dataset loaded into Research.");
    return;
  }
  const d = e.target.closest("[data-del]");
  if (d) {
    if (!confirm(`Delete dataset "${d.dataset.name}" from the server?\n\nThis removes the uploaded file and its history. This cannot be undone.`)) return;
    try {
      const res = await fetch("/api/datasets/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: Number(d.dataset.del) }) });
      const j = await res.json();
      if (!res.ok) { toast(j.error || "Delete failed"); return; }
      toast("Dataset deleted.");
      loadDatasets();
    } catch (err) { toast(isUnreachable(err) ? "Can't reach the local server." : "Delete failed: " + err.message); }
  }
}

// ---- Runs view ----
// Researchers just want the results of a past run, not a file browser: each row offers a
// single "Download results (.zip)" of that run's bundle (/api/bundle/<owner>/<run_id>).
async function loadRuns() {
  const el = $("runsList"); el.innerHTML = '<div class="empty-hint">Loading…</div>';
  const owner = state.user ? state.user.username : "";
  try {
    const items = (await (await fetch("/api/runs")).json()).runs || [];
    el.innerHTML = items.length ? items.map((x) => {
      const done = x.status === "done" || x.status === "incomplete";
      const zip = done
        ? `<a class="ghost small" href="/api/bundle/${encodeURIComponent(owner)}/${encodeURIComponent(x.run_id)}" download>${icon("download")} Results (.zip)</a>`
        : `<span class="muted small">${escapeHtml(x.status)}</span>`;
      return `<div class="card"><div class="card-main"><strong>${escapeHtml((x.question || "").slice(0, 90))}</strong>` +
        `<span class="muted">${escapeHtml(x.status)}${x.plan_mode ? " · planned" : ""} · ${fmtDate(x.created_at)}</span></div>${zip}</div>`;
    }).join("") : '<div class="empty-hint">No runs yet.</div>';
  } catch { el.innerHTML = '<div class="empty-hint">Failed to load.</div>'; }
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

// ---- Account view ----
function renderAccount(user) {
  const el = $("accountInfo");
  if (!user) { el.innerHTML = '<p class="muted">Accounts are disabled on this server.</p>'; $("changePwForm").classList.add("hidden"); $("logoutBtn").classList.add("hidden"); return; }
  $("changePwForm").classList.remove("hidden"); $("logoutBtn").classList.remove("hidden");
  el.innerHTML = `<p><strong>${escapeHtml(user.username)}</strong> · role: ${escapeHtml(user.role)}</p>` +
    `<p class="muted">Member since ${fmtDate(user.created_at)}</p>`;
}
async function changePassword(e) {
  e.preventDefault();
  const payload = { old_password: $("cpOld").value, new_password: $("cpNew").value };
  const r = await fetch("/api/account/password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(d.detail || "Change failed"); return; }
  toast("Password updated"); $("changePwForm").reset();
}

// ---- init -----------------------------------------------------------------

function init() {
  $("connectForm").addEventListener("submit", connect);
  $("reattachDismiss").addEventListener("click", () => $("reattachHint").classList.add("hidden"));
  $("disconnectBtn").addEventListener("click", disconnect);
  $("stopGpuBtn").addEventListener("click", stopGpu);
  $("storageBtn").addEventListener("click", openStorage);
  $("storageClose").addEventListener("click", () => $("storageModal").classList.add("hidden"));
  $("storageModal").addEventListener("click", (e) => { if (e.target.id === "storageModal") $("storageModal").classList.add("hidden"); });
  $("storageItems").addEventListener("click", (e) => { const b = e.target.closest("[data-path]"); if (b) deleteStorageItem(b.dataset.path); });
  $("modelSelect").addEventListener("change", (e) => selectModel(e.target.value));
  $("pullModelBtn").addEventListener("click", () => {
    const tag = prompt("Pull a model tag from Ollama (e.g. qwen3:32b, llama3.1:8b):");
    if (tag && tag.trim()) selectModel(tag.trim());
  });
  $("duoPushBtn").addEventListener("click", () => submitDuo("1"));
  $("duoSubmitBtn").addEventListener("click", () => submitDuo($("duoInput").value.trim() || "1"));
  $("duoInput").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submitDuo($("duoInput").value.trim() || "1"); } });
  $("authMethod").addEventListener("change", syncAuthMethod);
  $("createKeyCheck").addEventListener("change", syncNewKeyPass);
  // Re-load saved keys for whatever UCInetID is typed (keys are per-user).
  $("userInput").addEventListener("change", () => { if ($("authMethod").value === "ssh_key") loadCredentials(); });
  $("chatForm").addEventListener("submit", sendChat);
  $("chatStop").addEventListener("click", stopRun);
  $("datasetFile").addEventListener("change", (e) => { for (const f of e.target.files || []) uploadDataset(f); e.target.value = ""; });   // multi-attach: each file joins the bind-set
  $("datasetDir").addEventListener("change", (e) => { if (e.target.files && e.target.files.length) uploadFolder(e.target.files); e.target.value = ""; });
  $("datasetChips").addEventListener("click", (e) => { const x = e.target.closest("[data-clear-path]"); if (x) toggleBound(x.dataset.clearPath); });
  $("caseNoteChips").addEventListener("click", (e) => { if (e.target.closest("[data-clear-note]")) { state.caseNote = null; renderCaseNoteChip(); } });
  $("caseNoteFile").addEventListener("change", (e) => { const f = e.target.files[0]; if (f) attachCaseNote(f); e.target.value = ""; });
  // "＋ Data" popover: upload a file/folder, or pick a recent upload.
  $("dataMenuBtn").addEventListener("click", (e) => { e.stopPropagation(); toggleDataMenu(); });
  $("dataMenu").addEventListener("click", (e) => {
    const act = e.target.closest("[data-act]");
    if (act) {
      const target = { file: "datasetFile", folder: "datasetDir", note: "caseNoteFile" }[act.dataset.act];
      if (target) $(target).click();
      toggleDataMenu(false);
      return;
    }
    const rec = e.target.closest("[data-path]");
    // Toggle the file in/out of the bind-set; keep the popover OPEN so several can be picked at once.
    if (rec) { toggleBound(rec.dataset.path); }
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".data-menu-wrap")) toggleDataMenu(false); });
  $("downloads").addEventListener("click", (e) => {
    if (e.target.closest("[data-regen]")) { primeComposer("regen"); return; }
    if (e.target.closest("[data-rerun]")) { primeComposer("rerun"); return; }
    const tab = e.target.closest("[data-rtab]");
    if (tab) { switchResultsTab(tab.dataset.rtab); return; }
    const cell = e.target.closest("[data-url]");
    if (cell) { e.preventDefault(); openResultFile(cell.dataset.url, cell.dataset.kind, cell.dataset.name); }
  });
  $("filePreviewClose").addEventListener("click", () => $("filePreviewModal").classList.add("hidden"));
  $("filePreviewModal").addEventListener("click", (e) => { if (e.target.id === "filePreviewModal") $("filePreviewModal").classList.add("hidden"); });
  $("chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("chatForm").requestSubmit(); } });
  // Live-swap Send⇄Stop as the user types during a run / plan review.
  $("chatInput").addEventListener("input", updateComposerButton);
  { const ps = $("presetSearch"); if (ps) ps.addEventListener("input", () => renderPresetList(ps.value)); }
  $("modeSelect").addEventListener("change", onModeChange);
  { const rs = $("routeSelect"); if (rs) rs.addEventListener("change", onRouteChange); }
  $("newSessionBtn").addEventListener("click", newSession);
  $("sessionList").addEventListener("click", (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { e.stopPropagation(); deleteSession(del.dataset.del); return; }
    const item = e.target.closest("[data-id]");
    if (item) selectSession(item.dataset.id);
  });
  $("toggleLeft").addEventListener("click", () => togglePanel("left"));
  $("toggleRight").addEventListener("click", () => togglePanel("right"));
  $("leftLogoutBtn").addEventListener("click", doLogout);

  syncAuthMethod();
  applyUiPrefs();
  loadPresets();
  loadSkills();
  loadSessions();
  renderSessionList();
  renderChat();
  applyStatus({ status: "disconnected", model: "" });
  renderDatasetChips();
  loadDatasetChips();    // show prior uploads (incl. folders) as selectable chips
  restoreConnection();   // re-subscribe to a still-live HPC3 session after a reload / back-nav
}

// After a reload or accidental back-navigation the server-side Connection (SSH+GPU+vLLM)
// and any in-flight run usually SURVIVE — only the client forgot its id. If we stored one,
// check it's still alive and re-open the WebSocket, which replays the status, the event
// log, any pending Duo/Plan prompt, and the live centre bubble of an in-flight run.
async function restoreConnection() {
  let id = null;
  try { id = localStorage.getItem(CONN_KEY); } catch {}
  if (!id) return;
  try {
    const res = await fetch(`/api/connections/${id}`);
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
    // Re-bind the in-flight run's owner chat BEFORE the WS replays it, so its replayed
    // report/artifacts land in the chat that launched it — not the active one.
    try {
      const ro = JSON.parse(localStorage.getItem(RUNOWNER_KEY) || "null");
      if (ro && ro.connId === id && ro.sid) state.runSessionId = ro.sid;
    } catch {}
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
