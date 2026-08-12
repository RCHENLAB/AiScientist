# Content-aware multi-file bind-set (feature ②, + two ① enhancements)

Date: 2026-07-24 · Author: `claude` · Branch: `feat/content-aware-multifile` (off `main` @ 742b673)

Builds directly on feature ① (`src/bioagent/tools/dataset_inspect.py`: `peek_dataset` /
`describe_dataset` / `inspect_dataset`). Nothing here re-implements peek/describe — the module is
reused as-is, in-process (no new HPC3 dependency for triage).

## The problem

A run today binds exactly ONE data file (`LabRequest.dataset_path`) plus a separate TEXT slot
(`case_note`). Real rare-disease / genomics work needs a SET of data files (a VCF + a BED gene
panel + a second VCF). And pipeline routing keys off the file SUFFIX (`_primary_suffix` →
`run_dataset_smoke_analysis`'s `dataset_kind` → the PI router), so a `.txt`-named file that is
actually a VCF is mis-routed, and feature ①'s content understanding is never consulted at run time.

## The three phases

### Phase A — multi-file bind-set (foundation)

Data model — a **bound dataset** is one data file the run analyzes. The set is ordered; the
**primary** (highest `_primary_rank`: recognized-format priority → shallowest → largest, client
order as the tiebreak) drives every legacy `decisions["dataset_path"]` consumer so nothing
regresses. The full set is exposed as `decisions["datasets"]`.

- `LabRequest` gains `datasets: list[dict] | None` (each `{path, name?, role?}`), alongside the
  UNCHANGED legacy `dataset_path: str | None` and the SEPARATE `case_note: str | None`.
- `_select_bound_datasets(req)` normalizes the request into an ordered `list[dict]` of bound files:
  - `req.datasets` present & non-empty → use it (filtered to entries with a real `path`, primary
    ranked first);
  - else `req.dataset_path` → a single-entry list (EXACTLY today's behavior);
  - else → `[]`.
- `_run_lab` staging is generalized: the PRIMARY is staged exactly as one file is staged today
  (local file / local folder-primary / dfs3b remote — all three branches preserved, driven off the
  primary path), setting `dataset_path` / `hpc_primary` / `dataset_result` as before. Each SECONDARY
  is staged with a compact helper (`_ensure_local_dataset` for a readable local copy; `hpc_primary`
  recorded for a dfs3b file so an on-HPC step reads it in place). Every bound file (primary +
  secondaries) is recorded in `decisions["datasets"]`.
- Persistence/resume: `_write_run_state` writes `state["datasets"]`; `_prepare_continue` restores it
  into `resume_decisions` and `cont_req.datasets`. A resume reuses `resume_decisions` verbatim (the
  resume branch never re-stages), so the whole set round-trips. Legacy runs with no `datasets` key
  resume exactly as before (the single `dataset_path` still drives everything).

Back-compat contract: when a client sends only `dataset_path` (old clients, resume of old runs),
`_select_bound_datasets` yields a one-item list whose sole member IS that path, so the primary IS the
only file and behavior is byte-for-byte today's. `decisions["datasets"]` is additive — every existing
consumer keeps reading `decisions["dataset_path"]`.

Isolation: untouched. `RunState` keying (run_id + conversation_id), `begin_run`, `chat_running`
serialization, `_followup_target`/resume all keep working — the bind-set is a property of the
request/decisions, not of the run-scope machinery. `_followup_target`'s "different dataset → new
study" check still reads `req.dataset_path` (the primary), and the frontend always posts
`dataset_path` = primary alongside `datasets`, so the check holds.

### Phase B — content-triage overrides suffix routing ("真的覆盖")

`select_pipeline` gains `content_modality` + `content_confidence` params:

- content modality is a real modality (not `""`/`unknown`) AND confidence is not `low` →
  `matches = [p for p in library if p.data_type == content_modality]` (with a small alias map, e.g.
  `variant`→`variants`, `single_cell`→`scrna`):
  - `matches` non-empty → routing is RESTRICTED to that modality bucket. One match → returned
    deterministically (skip the LLM); several → the LLM router runs over `matches` only (content
    picks the bucket, the LLM picks within it). This is the real override: a scanpy pipeline can
    never be chosen for a file content says is variants.
  - `matches` empty (e.g. content=`tabular`/`text`, no matching pipeline) → fall through to the
    full-library LLM router.
- content modality unknown / low-confidence / absent → full-library LLM router (today's behavior).
  This is the **suffix fallback**: the LLM hint is still the suffix-derived dataset profile.

`content_modality` is threaded from `ResearchLab.run()` via `self.ctx.decisions["content_modality"]`
(set by Phase C). `drop_conflicting_pinned` is unchanged and stays coherent: it drops pinned
pipelines whose `data_type` differs from the (now content-derived) `chosen.data_type`.

### Phase C — auto-describe at run start (no extra GPU)

In `_run_lab`, AFTER the lazy GPU/vLLM is ensured ready for the run (the model is warming for the run
regardless — no extra provisioning) and `complete_fn` exists, run feature ①'s `describe_dataset` on
each bound file (peek local staged copy, or `_peek_dataset_any` for a remote-only path), with a
`think=False` chat_fn over this session's tunnel (same contract as `/api/dataset/describe`). Each
`decisions["datasets"]` record gets a `description`; the PRIMARY's `likely_modality` /`confidence`
become `decisions["content_modality"]` / `["content_confidence"]` for Phase B. The user sees a
"what the run understood it received" line per file. Best-effort: any failure leaves routing on the
suffix fallback and never fails the run. Skipped on resume (routing guidance is already restored).

## Frontend

Minimal, backward-compatible generalization of the single-dataset "+ Data" attach: the recent-uploads
list becomes a multi-select (toggle files into the bind-set), the composer renders one removable chip
per bound file, and the run POSTs `datasets: [{path, name}]` AND `dataset_path` = primary (so old
server code paths and `_followup_target` keep working). Each chip shows the file's ① gist when known.

## Wired vs deferred

Wired: the bind-set data model end-to-end (request → staging → decisions → run_state → resume),
content-over-suffix routing in `select_pipeline`, run-start auto-describe, and the minimal
multi-attach frontend.

Deferred (documented): a richer multi-file composer UX (drag-to-reorder, per-file role picker);
staging every secondary as a first-class analysis input for the HPC analysis/VEP executors (today
secondaries are reachable by `run_code` via the exposed uploads tree / dfs3b, and the PRIMARY drives
the scanpy/VEP tools — matching today's single-primary model); and multi-file-aware `_followup_target`
diffing (it still compares the primary only).
