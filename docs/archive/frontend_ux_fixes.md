# Frontend UX fixes — batch (branch `fix/frontend-ux-batch`)

Owner: Yijun · started 2026-07-01 · target UI: `frontend/console/` (the real research console).

Seven researcher-facing defects in the HPC3 console. Each item lists the root cause,
the fix, and the files it touches. Checkboxes track progress.

---

## 1. Cross-conversation result/download leak
- [x] **Symptom:** While chat A's run is in flight, the user switches to a new chat B.
  When A finishes, its report + downloads attach to B (the *current* chat), not A.
- **Root cause:** results bind to `state.runSessionId`, but that id is lost on a
  reload/reconnect (WS replay of a live run doesn't restore it), so it falls back to
  `state.activeId` (whatever chat is on screen). The live streaming bubble is also
  appended to the *visible* stream, so switching chats detaches it.
- **Fix:** persist the run owner (`{connId, sessionId}`) so `restoreConnection()`
  re-binds it after a reload; make all result/artifact writes go to the owner id only;
  never render into the DOM unless the owner chat is the active one.
- Files: `frontend/console/app.js`.

## 2. Streaming bubble freezes / stops refreshing
- [x] **Symptom:** the streamed answer sometimes "sticks" with no live output.
- **Root cause:** same as #1 — after switching chats (or reload) the streaming element
  is detached from the DOM, so tokens update an invisible node.
- **Fix:** decouple stream *state* (text/thinking/progress) from the DOM; re-mount and
  repaint the live bubble whenever the owner chat becomes visible (`renderChat`).
- Files: `frontend/console/app.js`.

## 3. Collapse `run_code: ok` spam; show final code; per-step summary
- [x] **Symptom:** the chat is flooded with `⚙️ Running run_code…` / `↳ run_code: ok`.
- **Fix:**
  - Move per-tool run_code chatter into the collapsible activity ("thinking") log, out
    of the always-visible key-progress feed.
  - Capture the last successful `run_code` body per step and render it as a collapsed,
    syntax-formatted code block in the conversation.
  - On step acceptance, render a concise **step summary** (result · quality via the
    Critic's score/verdict · significance via the Critic critique).
- Files: `src/bioagent/gateway/app.py` (`_lab_event_to_chat`), `src/bioagent/agents/research_lab.py` (add critique to the `critic` event), `frontend/console/app.js`, `frontend/console/styles.css`.

## 4. Event/error log duplicates the chat — move it to the results folder
- [x] **Symptom:** the right-panel "Event & error log" repeats what the chat already
  shows; researchers don't need it on screen.
- **Fix:** hide the log panel from the researcher UI; write the full event log into the
  run's results bundle (`process/event_log.txt`) so it's still recoverable.
- Files: `frontend/console/index.html`, `frontend/console/styles.css`, `frontend/console/app.js`, `src/bioagent/gateway/app.py`.

## 5. Unify preview icons → folder/Material style
- [x] **Symptom:** emoji file/folder icons look inconsistent and dated.
- **Fix:** adopt Google **Material Symbols** (fonts.google.com/icons) for the nav rail,
  file rows, and folder groups; drop the ad-hoc emoji set.
- Files: `frontend/console/index.html`, `frontend/console/app.js`, `frontend/console/styles.css`.

## 6. Runs tab — one zip download per run
- [x] **Symptom:** the Runs tab opens a full file grid; researchers just want the results.
- **Fix:** each run row offers a single "Download results (.zip)" action (the existing
  `/api/bundle/<owner>/<run_id>` endpoint); drop the per-run file browser.
- Files: `frontend/console/app.js`, `frontend/console/index.html`.

## 7. Folder upload (nested, multi-level), Claude-style
- [x] **Symptom:** only single-file upload; datasets tab asks for a server path.
- **Fix:**
  - Add a folder picker (`webkitdirectory`) that uploads a whole tree preserving nested
    relative paths; keep single-file upload too.
  - The Research dataset field shows uploaded **names** (chips), not a manual path.
  - Allow adding more folders/files later in the same session; every prior upload stays
    accessible to the agent (`run_code` sees the whole `uploads/` tree via
    `BIOAGENT_UPLOADS`).
  - Datasets tab lists folders as one entry (kind=folder).
- Files: `src/bioagent/gateway/app.py` (upload rel-path + folder registration + sandbox
  wiring), `src/bioagent/agents/sandbox.py` (`BIOAGENT_UPLOADS`), `src/bioagent/tools/datasets.py`
  (dir-aware smoke analysis), `frontend/console/index.html`, `frontend/console/app.js`,
  `frontend/console/styles.css`.
