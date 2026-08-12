# Fast chat path (answer-first ReAct) + inline Mermaid in the console

**Date:** 2026-07-20 · **Branch:** `feat/fast-chat-path-and-inline-mermaid` (off `main` @ `a6e26a1`)
· **Author:** `claude` · **Status:** built, partially verified, **not deployed, not merged**

Two features that ship together: a route where a question gets a fast streamed answer instead of the
full research pipeline (A), and inline diagram rendering so the answer can *show* a workflow rather
than describe it (B). B is what makes A's answers worth reading.

---

## 1. The problem, re-verified

The findings handed to me were gathered 2026-07-17. I re-checked each against the tree at `a6e26a1`
before designing. **All four held.** Two additions materially changed the design — see §5.

| Claim | Verified | Where |
|---|---|---|
| No plain-chat path; every message runs the full lab | ✅ holds | `frontend/console/app.js` `sendChat()` → `POST /api/lab` → `_dispatch_lab` → `_run_lab` |
| `mode` is Axis A (single / team / auto), not chat-vs-research | ✅ holds | `LabRequest.mode`, `app.py:1650` |
| `renderMarkdown()` is a hand-rolled pipeline: fences stashed → headings → lists → `mdTables()` → bold → italic | ✅ holds, exactly | `app.js:110` (now `:190`) |
| `mmdc` not on prod; only graphviz `dot` — so render client-side | ✅ accepted as given, **not independently re-checked** (no prod access from this worktree) | — |

---

## 2. Feature A — routing between fast path and research

### The decision: an explicit toggle, no classifier

A new composer control, `#routeSelect`, sends `LabRequest.route ∈ {"research", "chat"}`. `"research"`
is the default; anything unrecognised (including an older client that omits the field) falls back to
research, so existing behaviour is bit-for-bit unchanged.

This is deliberately **not** a classifier. The failure mode of a misroute is asymmetric:

- chat → research misroute: the user waits too long. Annoying, obvious, self-correcting.
- research → chat misroute: the model produces a **fluent, confident answer with no analysis behind
  it**, and nothing downstream flags it. The report-fabrication work already in this codebase
  (`verify_report_facts`, the 4-layer defence in `research_lab.py`) exists precisely because this
  class of failure is invisible to the reader.

The fast path's system prompt carries a matching guardrail: it is told it *cannot* run the pipeline
and must say so and point at Research mode rather than improvise. That is a mitigation, not a
substitute for the explicit toggle.

Axis A (`mode`) and Axis B (`route`) are orthogonal: `mode` picks one scientist vs a Virtual-Lab
team, and only applies *inside* the research engine. In chat mode the research-only controls (mode,
Plan first, Bypass) are **disabled and dimmed**, not hidden, so the user can see what they'd get by
switching back.

### The loop

`src/bioagent/agents/quick_chat.py` — no PI, no agenda, no Critic, no report bundle:

```
stream an answer  →  (if the model asked for a tool) run it  →  stream again
```

Bounded by `QuickChatConfig`: 4 model turns, 6 tool calls, 12 carried history messages.

Two decisions make it *feel* like Claude's ReAct rather than a smaller lab:

1. **Tokens are pushed as they arrive**, not after the turn completes. Test
   `test_tokens_are_emitted_before_the_turn_ends` pins the ordering, because "answer-first" is a
   latency claim and a test that only checks the final string would pass on a fully-buffered
   implementation.
2. **`think=False` by default.** The served Qwen3.6 is a reasoning model; with the trace on, the
   first seconds of output are tokens the reader cannot use. This is the opposite default from the
   pre-existing `chat_stream()`, and it is pinned by a test.

The system prompt's first instruction is *answer first, never a preamble* — without it Qwen opens
with "Let me look that up…", which streams instantly and says nothing, defeating the whole point.

### The streaming/protocol change

**No new WebSocket event types.** The fast path reuses the existing
`chat_start → chat_token / chat_thinking / lab_progress / run_status → chat_done` protocol, so Stop,
reconnect-replay (`stream_replay_payloads`), and per-run demuxing (`RunState.tag`) keep working with
zero client changes. Tool chatter goes to the collapsible activity log (`chat_thinking`), matching
the split `_lab_event_to_chat` already makes.

Wire-level additions, both backward-compatible:

| Field | Direction | Notes |
|---|---|---|
| `LabRequest.route` | client → server | `"research"` (default) \| `"chat"` |
| `LabRequest.history` | client → server | prior turns; **only the fast path reads it** — a study is scoped by question + dataset, and the follow-up router reads persisted run state instead |

New transport primitive: `vllm_client.chat_tools_stream()`. Neither existing function could do this
job — `chat_stream()` can't call tools, `chat_tools()` isn't streaming (its first token lands only
after the whole turn generates). The new one is both, and reassembles fragmented `delta.tool_calls`
(name arrives once, `arguments` dribbles in as JSON string fragments) into the same shape
`chat_tools()` returns, so callers parse it identically.

### What the fast path deliberately cannot do

`build_quickchat_catalog()` is a hand-picked allow-list — `literature_search`,
`map_phenotype_to_hpo` — not a filter over the full catalog, because a filter would silently admit
every future tool that happened to match. Excluded on purpose: `run_code` and the whole
scanpy/variant/phenotype line (minutes-to-hours of HPC3 compute, and a chat turn has no run bundle
to write into), and `make_schematic` (it writes a figure *artifact* into `<run>/artifacts/figures`,
which a chat turn does not have). A test asserts each of those nine names is unreachable.

### A bug found while wiring this up

`/api/chat/inject` (mid-run steering: type while a run executes and the note is folded into the
remaining steps) is guarded only by `conn.chat_running`. A chat turn sets that flag too — but a chat
turn has no steps, so nothing drains `conn.injections`. A note typed during a chat reply would sit
in the queue and then be **silently picked up by the next research run**, steering a study with a
sentence the user aimed at a chat message.

Fixed by tagging each run: `RunState.kind ∈ {"research", "chat"}`. `/api/chat/inject` now 409s
against a chat turn with a readable message (the client already toasts it), and `_run_quick_chat`
drains any stale queue on entry.

### Two deliberate non-shares with `_run_lab`

- **No `_remember_run`.** A chat turn is not a run. Registering one as the conversation's last run
  would make the *next* research message try to amend a report that was never written.
- **No `_with_recovery` retry.** `_lab_llm` heals a dropped tunnel and retries the whole call —
  right for an atomic completion, wrong for a stream whose first half is already in the user's
  browser, where a retry would replay the answer's opening. A dropped fast-path stream surfaces as a
  chat error; retyping costs seconds here, which is the premise of the path.

---

## 3. Feature B — inline Mermaid

A ```` ```mermaid ```` fence in any assistant message renders as a diagram in the chat.

### How it renders

`mermaid.min.js` **v11.12.0**, vendored to `frontend/console/mermaid.min.js` (2 748 992 bytes,
sha256 `07e37dfa97b337ccc85365d57eddf99b9706f09db3b59b260d0333b23b343c4b`). Copied from the npm
package inside the locally-installed Antigravity IDE (`node_modules/mermaid@11.12.0`) — **not
fetched from a CDN**, since prod has no guaranteed egress and the vendored file must work offline.
Its dist build ends with `globalThis["mermaid"] = …`, so it exposes a clean global.

Because it is 2.7 MB it is **loaded lazily**, on the first message that actually contains a diagram.
A user who never sees one never pays for it.

The fence is stashed by the *existing* code-fence machinery in `renderMarkdown()` — same escaping,
same protection from the inline bold/italic rules — but tagged `.md-mermaid` so `renderMermaidIn()`
can upgrade it to SVG afterwards. `messageEl()` calls that upgrade fire-and-forget, so a slow or
absent bundle can never delay painting the message.

**Not called from `repaintStream()`**: mid-stream a diagram is half-typed and therefore invalid, and
re-parsing every frame would flash parse errors. The live bubble is replaced by the persisted
message at `chat_done`, which is where diagrams land.

### How it is sandboxed

Four layers, and the escaping guarantee of `renderMarkdown` is never weakened at any point:

1. **Escaped first.** The source goes into the DOM as escaped text in a `<pre>`. Until mermaid
   succeeds the page contains no markup from model text.
2. **`securityLevel: "strict"`** — no click handlers, no scripts.
3. **`htmlLabels: false`.** *This is not redundant with layer 2.* Measured in the harness: with HTML
   labels on, a node label of `A["<img src=x onerror='…'>"]` renders as a `<foreignObject>`
   containing a **real `<img>` element**. Strict mode did strip the `onerror` — no script executed,
   confirmed — but the `<img src>` survived, i.e. model text could still emit markup that fires an
   outbound request. Turning HTML labels off makes mermaid draw labels as SVG `<text>`, removing the
   surface instead of sanitizing it.
4. **`sanitizeMermaidSvg()`** on the output: parse in an inert `DOMParser` document (which neither
   fetches nor executes), drop `script/img/image/iframe/object/embed/foreignObject/link/animate/set/use`,
   strip every `on*` handler and `javascript:` / `data:text/html` attribute value. Non-`<svg>` output
   is rejected and treated as a parse failure.

`<style>` is deliberately **kept**. Mermaid emits its whole theme as an inline `<style>` inside the
SVG; stripping it hardens nothing and just produces an unstyled diagram — I did exactly that at
first and measured the result (black boxes, labels detached from their shapes). Only the two CSS
constructs that can make an outbound request (`@import`, `url(`) are blanked, which is the actual
risk, since a diagram *can* inject CSS via `classDef` / `style` directives or a `%%{init}%%`
`themeCSS` block.

**Failure degrades to the source, never to injected markup and never to a blank message**: a broken
diagram keeps its text, gets a `.md-mermaid-fallback` label ("diagram source (could not be
rendered)") and the parse error as a tooltip.

### Reconciliation with the existing schematic tool

`tools/schematic.py` (`render_mermaid` / `render_dot` / `mermaid_available`) and
`registry.make_schematic_tool` are **untouched** — verified by a test. They render figure *artifacts*
via graphviz into a run bundle, for the report/PDF. Inline chat mermaid is the complementary
capability: model text → browser SVG, no server dependency, no artifact. Nothing about this change
depends on `mmdc`.

### Theming

Mermaid is initialized with `theme: "base"` and `themeVariables` read from the console's own CSS
custom properties. The console is a single warm ("tea") light palette with no dark mode, so the
hardcoded dark theme I started with dropped a black box into a beige page. Diagrams now inherit the
palette and keep matching it if it is retuned.

---

## 4. What was verified, and how

Every claim below is backed by a command I actually ran in this worktree on 2026-07-20.

### Automated — `tests/test_quick_chat.py`, 22 tests, all passing

`PYTHONPATH=src /Users/yijunsun/miniconda3/bin/python -m pytest tests/test_quick_chat.py -q`
→ `22 passed in 0.05s`

Covers: single-turn answer with no tools; token emission ordering (the latency claim); tool result
fed back as `role:tool` with the matching `tool_call_id` (vLLM/OpenRouter 400 without it); a raising
tool becoming data for the model rather than a dead turn; unknown-tool reporting; turn-limit
enforcement *and* its flag; tool-budget exhaustion telling the model in-band; mid-stream cancel
stopping promptly (not after draining the generator); history trimming; thinking/content separation;
`_parse_args` totality on 5 malformed inputs; SSE assembly of fragmented tool-call deltas; the
`think=False` default; parallel tool calls keeping their index order; the fast-path catalog
allow-list *and* its nine exclusions; `make_schematic` still present on the research path; and the
four load-bearing clauses of the system prompt.

### Regression — no new failures

Full suite, excluding the 15 files that import `gateway.app` (needs `paramiko`, absent locally):

| | passed | failed | errors |
|---|---|---|---|
| baseline (`git stash`, same command) | 591 | 9 | 10 |
| with this branch | **613** | 9 | 10 |

+22 = exactly the new tests. Every pre-existing failure/error is `ModuleNotFoundError: paramiko`.

### Frontend logic — real functions extracted from `app.js` with `sed`, run under `node`

10/10 pass. Mermaid fence → `.md-mermaid` `<pre>`; source HTML-escaped (an `<img onerror>` label
comes out as `&lt;img`); mermaid body not mangled by the bold/italic rules (`a_b_c` and `**x**`
survive); plain and unlabelled fences still `.md-code`; uppercase ```` ```MERMAID ```` matches; two
diagrams in one message both stash; **and the pre-existing behaviours still work** — GFM tables,
headings, bold/italic, `target_sum` not italicised, raw `<script>` in prose still escaped.

### Browser — served over `python3 -m http.server`, driven through the Browser pane

- **Mermaid probe:** v11.12.0 loads, exposes `window.mermaid`, renders a 5-node flowchart to a
  12 028-char SVG under `securityLevel:"strict"`.
- **Chat harness** (real extracted `renderMarkdown` + `renderMermaidIn` + real `styles.css`), 4
  messages: good diagram / broken diagram / injection attempt / mixed table+code+diagram →
  `ok=3 fallback=1 svg=3 injected_img_or_script=0 pwned=false`, plus `imgs=0 foreignObject=0`,
  0 elements carrying an `on*` attribute, and mermaid's 3 `<style>` elements preserved. The
  injection case's `window.__PWNED` was never set. Fallback case confirmed to keep its source, its
  `::before` label, and the parse error in `title`.
  *Before* the `htmlLabels:false` + sanitizer fix the same harness reported
  `injected_img_or_script=1` with a live `<img src="x">` in the DOM — that is how layer 3 was found.
- **Real console page** (`frontend/console/` served whole): loads with **no console errors**;
  `#routeSelect` present, defaults to `research`; toggling to Chat disables + dims mode/Plan/Bypass
  and swaps the composer placeholder; toggling back restores all of it; the choice persists onto the
  session object.
- **End-to-end payload:** with `fetch` stubbed, a real `sendChat()` in chat mode POSTs
  `route:"chat"` and `history:[{user:"earlier q"},{assistant:"earlier a"}]` alongside the question.

### Visual

Screenshots taken of the rendered flowchart (matches the console palette, sans-serif labels after
the `font: inherit` fix) and of the composer in chat mode.

---

## 5. Things I found that the 2026-07-17 findings did not mention

Both changed the implementation:

1. **The token-streaming protocol already exists and is unused by any streaming producer.**
   `chat_token` / `chat_thinking` / `chat_start` / `chat_done` are fully implemented on both sides —
   `streamToken()`, `_track_stream()`, `stream_replay_payloads()` — but today only ever carry
   whole-message pushes from the lab. So Feature A needed **no client protocol work at all**; it
   plugs into a channel that was already built, tested, and reconnect-safe. This is why the
   frontend diff is a UI toggle and a markdown change, not a streaming client.

2. **`vllm_client.chat_stream()` exists, is fully implemented, and is dead code.** Nothing in
   `src/` calls it; its only references are its own docstring and two tests. It streams but cannot
   call tools, which is why the fast path needed a new `chat_tools_stream()` rather than reusing it.
   Worth a decision from Yijun: `chat_stream` is now the *only* dead function in that module, and
   either it should be deleted or the two overlapping streamers should be merged.

---

## 6. What is NOT verified — read this before merging

- **No live model has ever run this loop.** Every fast-path test uses a scripted `stream_fn`. The
  loop is unexercised against real Qwen3.6: whether it actually leads with a substantive sentence,
  whether it emits usable mermaid unprompted, and whether it respects the "you cannot run analyses
  here" instruction are all **unmeasured**. Given the documented history of this model returning
  empty content when the reasoning trace eats `max_tokens`, an empty-reply path is implemented and
  unit-tested — but not observed in production conditions.
- **Nothing in `gateway/app.py` was executed.** `paramiko` is absent from every interpreter on this
  machine, so `_fast_path`, `_run_quick_chat`, `_quickchat_stream_fn`, and the two new `LabRequest`
  fields have **never been imported, let alone run**. What I could check statically: the module
  parses, all three functions exist, `_dispatch_lab` calls both, `LabRequest` carries both fields,
  and an AST pass found no unresolved global names in the new functions. That is not a substitute
  for running them. The 15 gateway test files that would cover this could not run locally.
- **The `mmdc`-absent-on-prod claim was taken on trust**, not re-checked — no prod access here. It
  does not affect the design (nothing added depends on `mmdc`).
- **`securityLevel:"strict"` was probed with one injection vector**, not fuzzed. Layers 3 and 4 are
  belt-and-braces precisely because layer 2 was measured to be insufficient once.
- **The vendored mermaid bundle was not independently checksummed against the upstream npm
  registry.** It came from an installed application's `node_modules` with a matching
  `package.json` (`mermaid@11.12.0`), and I recorded its sha256 above, but that is provenance by
  proximity. If that is not good enough for a 2.7 MB third-party file in prod, re-vendor it from
  `npm pack mermaid@11.12.0` and diff.
- **No browser other than the Browser pane's Chromium was tested**; no mobile layout check.
- **CI will not vet this.** Pushes to this repo bypass required status checks.
- **Not deployed.** `scripts/sync_deploy.sh` is Yijun's to run, and it refuses a dirty tree.

## 7. Open questions for Yijun

1. Should the fast path be reachable **without a GPU session**? Today it lazily provisions one like
   the lab, so the *first* chat message on a cold session still waits minutes for a GPU — which is
   the one case where the fast path is not fast. A small always-on model, or routing chat to a
   different endpoint, would fix it; both are bigger decisions than this branch.
2. `chat_stream()` (§5.2): delete, or merge with `chat_tools_stream()`?
3. The chat catalog is two tools. Literature Q&A (paper-qa, per the literature-backend decision) is
   the obvious third once `literature_qa` exists — the loop is already tool-capable and needs no
   change to accept it.
