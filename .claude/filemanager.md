# File Manager — structure change log

Purpose: a shared, durable record of **file/folder STRUCTURE changes** (new / removed /
moved / renamed files and directories) so any later session or teammate can tell **who
added what, when, and why** — instead of finding an unfamiliar path with no context.

How to use (every session must follow):
- **Before** structural work, read this file to see what exists and why.
- **After** adding / removing / moving / renaming any file or directory, append an entry to
  the log below (newest first). Keep content-only edits out unless they matter for orientation.
- One entry per change-set: date, author (which session/agent or person), the path(s), the
  change type, and a one-line why. Note if a path is intentionally NOT committed.

Author tags: `claude` = a Claude Code session; `user` = a human teammate; `claude+user` =
created by Claude then reworked by a human.

---

## Change log (newest first)

- 2026-08-08 · `claude` · (worktree `ziyaoma-pr-merge-status-544fde`)
  **renamed on HPC3 (not a repo path):** `/dfs3b/ruic20_lab/software/bioagent` →
  `.../software/AiScientist`, with `software/bioagent` left as a **symlink** so prod's `.env` and
  out-of-repo scripts keep resolving (same zero-downtime trick as the `BIOAGENT_*`/`AISCIENTIST_*`
  env aliases). 103 G of containers/weights, same-filesystem `mv`, no jobs running, all 7 `.sif`
  verified through both names. 33 hard-coded `software/bioagent` paths updated across
  `gateway/settings.py`, `deploy/{analysis,report,paperqa,scgpt,vep,lirical}/*`, and their READMEs.
  **added** `docs/hpc3_assets.md` — the inventory of everything we own on HPC3 (containers, model
  weights, 241 G of annotation DBs in the lab-shared `software/reference`, what built each and how
  to rebuild it), because almost none of it is in the repo.
  Not `/dfs3b/ruic20_lab/AiScientist`: that top level is `drwxr-s--- ruic20` with no group write,
  and `newgrp`/`sg` do not help (supplementary groups already count) — `software/` is the
  group-writable public dir.

- 2026-08-07 · `claude` · (worktree `ziyaoma-pr-merge-status-544fde`)
  **added** `src/bioagent/gateway/hpc_gc.py`, `deploy/hpc3/aiscientist_temp_gc.sh`,
  `docs/hpc3_storage_layout.md`, `tests/test_hpc_temp_gc.py` — HPC3 had **no** cleanup at all for
  per-run process files. Results mirror back to the eyeserver and the only GC in the product
  (`app._expire_old_checkpoints`) sweeps the eyeserver's local run bundles; the cluster side just
  grew, inside each member's PERSONAL `/dfs3b/ruic20_lab/<ucinetid>/` dir, where automating a
  `rm -rf` would never be safe. Everything AiScientist generates now goes to ONE shared root
  (`BIOAGENT_HPC_SHARED_ROOT`): `Temp/<user>/` for process files (swept after
  `BIOAGENT_TEMP_TTL_DAYS`, default 3 — a unit dies only when its whole subtree is cold, so a live
  job can't be half-deleted), `uploads/<user>/` for raw data and `pysrc/<user>/` for the synced
  source, both never swept. The sweep is **submitted as a Slurm batch job**, so the login node only
  runs `sbatch` (RCIC: login nodes are for logging in and submitting, not doing). Personal lab dirs
  are read/browsed and **never** auto-deleted. `deploy/hpc3/` is a new dir (first HPC3-side ops
  script that isn't part of a container build). **Content edits, same change-set:**
  `gateway/settings.py` (`shared_root` + `temp_ttl_days`), `gateway/app.py`
  (`_temp_base`/`_shared_dir`/`_hpc_uploads_dirs`, all run + scratch paths repointed,
  `_prepare_shared_storage` + `_submit_temp_sweep` + `_hpc_temp_gc_loop`, storage panel lists all
  three areas and its delete guard covers them), `gateway/scgpt_runner.py` (docstring),
  `tests/test_uploads_hpc.py` + `tests/test_bind_set.py` (new upload/pysrc paths).

- 2026-08-05 · `claude` · (worktree `adaptive-kg-status-40c9b8`)
  **added** `src/bioagent/tools/phenotype_evidence.py` + `tests/test_phenotype_evidence.py` — the
  literature EVIDENCE track, the runner `docs/paperqa2_evidence_layer_contract.md` had left as a
  placeholder. It grades a gene–disease association from `deep_literature` (PaperQA2) into a ClinGen
  tier, and the point of the module is that the tier is NOT the model's word: retrieval decides
  existence (no passage ⇒ NONE), `evidence_ceiling()` caps the grade at what the retrieved passages
  can support — counting INDEPENDENT sources, not chunks, so one heavily-chunked paper cannot look
  like replication — and every graded claim keeps the passage it came from. Most of the test file is
  refusals. **Content edits, same change-set (no new files):** `tools/phenotype_dx.py` gains
  `adjudicate()` (one ranked differential, literature weighted 0.65 vs LIRICAL 0.35, so a retrieved
  refutation sinks a curated call) + `diagnose()` (both tracks end-to-end; answers from the
  literature when LIRICAL is not staged / not curated, which is what "cannot diagnose" used to mean)
  + the `diagnose_disease` tool; `agents/registry.py` binds that tool AFTER routing so it composes
  the ROUTED `run_lirical`/`deep_literature` and follows them onto HPC3. `posttest_prob` is still
  never rewritten — `final_score` is a separate, separately-named ranking axis.

- 2026-08-05 · `claude` · (worktree `eyeserver-gpu-request-check-4b9621`)
  **added** `reports/AiScientist-能力全览-zh.pptx` + `reports/AiScientist-Capability-Overview-en.pptx`
  — a 43-slide capability deck for USERS: what research the system can do today (six lines),
  how a run works, the analysis engine in technical detail, and why the output is trustworthy.
  Both languages are rendered from ONE set of slide definitions (generator kept in the session
  scratchpad, `capdeck/`: lib.js layouts + content1-3.js bilingual content + build.js, which
  fails the build if the two slide counts ever diverge). Every number in it comes from the
  codebase or a measurement on HPC3 / the eyeserver — and one slide is nothing but the current
  limits, so a user meets a gap on a page rather than mid-run.

- 2026-08-03 · `claude` · (worktree `eyeserver-gpu-request-check-4b9621`)
  **added** `src/bioagent/agents/tool_source.py` + `tests/test_tool_source.py` +
  `scripts/probe_tool_audit.py` — `read_tool_source`, letting the agent read the SOURCE of the
  tools it calls. Motivation is concrete: `run_de`'s 50-gene cap, `run_enrichment`'s constant
  background and `resolution=1.0` all survived seven weeks behind green tests and self-consistent
  reports. A tool's description states intent; only the body states behaviour, and the model could
  only ever see the first. Returns the body, the declared description/schema next to it (so the two
  can be compared), and a `defaults` list of every `args.get(x, <literal>)` — the values nobody
  chose. Read-only on purpose: a tool that rewrote its own implementation mid-run would make that
  run unreproducible. The probe measures whether the model ACTS on it (2 defect scenarios + 1
  control), because a capability the model ignores is worth nothing.
  **added** `src/bioagent/tools/scrna_advanced.py` + `tests/test_scrna_advanced.py` — the five
  missing analysis steps (doublets, integration, pseudobulk DE, composition, marker annotation).

- 2026-08-02 (latest) · `claude` · (worktree `eyeserver-gpu-request-check-4b9621`)
  **added** `skills/annotate_clusters_by_markers_v2/{SKILL.md,reference.py}` — v2 of the
  marker-annotation skill, written against Rui Chen's real protocol. v1 counted top-25 DE genes
  against a marker list and took the argmax, which cannot handle shared markers (LAMP3 in both
  AT2 and DC; SLC1A3 in both Müller glia and astrocyte) and so produces confident wrong labels.
  v2 scores signatures, treats the z-argmax as a first pass, and decides on RAW marker
  expression, leaving incoherent clusters `Unassigned`. Versioned via `supersedes:` in the
  frontmatter, so v2 is what the manifest advertises and v1 stays on disk for rollback — the
  first live use of the induction versioning mechanism.
  **added** `tests/test_resolution_selection.py`, `tests/test_skill_versioning_live.py`.

- 2026-08-02 (later) · `claude` · (worktree `eyeserver-gpu-request-check-4b9621`)
  **added** `src/bioagent/agents/context_budget.py` + `tests/test_context_budget.py` — run-scope
  context management. `ResearchHarness._trim_history` already budgeted WITHIN a step; nothing
  measured the RUN scope, where `_accepted_findings_block` is rebuilt into every step's brief and
  grows linearly. The new module measures that carried block against a share of the served window
  and compacts it. Deliberately split: every DECISION is arithmetic here (thresholds, which rounds
  fold, what is pinned), the model only writes digest prose, and `compact_block` re-attaches the
  artifact pointers from the original rounds so compaction can lose detail but never provenance.
  Gated OFF (`BIOAGENT_CONTEXT_MANAGEMENT`); `POST /api/lab/compact` is the compact command, a
  control flag on the RunState rather than a note queued through `/api/chat/inject`.

- 2026-08-02 · `claude` · (worktree `eyeserver-gpu-request-check-4b9621`)
  **added** `src/bioagent/agents/skill_induction.py` + `tests/test_skill_induction.py` — skill
  induction, the half of `skills.py`'s "grown by induction" claim that was never built. At the end
  of a run an accepted `run_code` procedure is generalized into a `SKILL.md` + `reference.py`.
  IMPORTANT for anyone browsing paths later: induced skills are written to a SEPARATE root
  (`BIOAGENT_INDUCED_SKILLS_DIR`, else the connection workspace's `_induced_skills/`), **never**
  into the repo's git-tracked `skills/` — a model editing shipped source is a different and worse
  thing than one leaving a template in its workspace. `skills.py` now loads both roots with curated
  winning on a name clash, and `register_skill()` adds an induced skill to the in-process library
  additively. Gated OFF (`BIOAGENT_SKILL_INDUCTION`), and the flag alone does nothing without a
  directory. Most of the test file is refusals: unsafe name, uncompilable code, oversized body,
  name collision, existing folder, traversal.

- 2026-08-02 · `claude` · (worktree `eyeserver-gpu-request-check-4b9621`)
  **added** `tests/test_multi_cycle.py` — the outer multi-CYCLE loop (`ResearchLab._run_campaign`,
  `LabConfig.max_cycles`, `BIOAGENT_MAX_CYCLES`). Where hypothesis-driven exploration reacts to ONE
  step inside a cycle, a cycle re-plans wholesale from what the previous cycles found, and the
  manuscript is written once over every cycle's rounds. No new source file — the loop lives in
  `agents/research_lab.py` next to the code it drives. Most of the test file is TERMINATION: each
  deterministic exit (max_cycles / PI declines / no-progress re-plan / nothing-left-to-chase /
  re-plan failure / cancel) gets its own case, because an outer loop whose exit condition is an LLM
  opinion is how a run costs a weekend of GPU time. `max_cycles=1` (default) keeps the old path.

- 2026-07-31 · `claude` · (worktree `eyeserver-gpu-request-check-4b9621`)
  **added** `src/bioagent/agents/hypotheses.py`, `tests/test_hypothesis_exploration.py`,
  `scripts/probe_exploration.py` — hypothesis-driven exploration, the plan's first mid-run GROWTH
  path. Until now the agenda was drafted once (before any data was seen) and could afterwards only
  shrink (pre-flight skip / post-step prune / plan review), so a result contradicting the plan's
  premise had nowhere to go and the system could never open a research path it did not start on.
  `hypotheses.py` is the ledger (falsifiable claim = statement + prediction + discriminating test,
  plus its adjudication); `research_lab._explore_after_step` is the LLM turn and the deterministic
  guards; `dag.LabPlan.extend`/`next_id` let the DAG grow a node that DEPENDS on the step that
  provoked it. `probe_exploration.py` is the model A/B harness — it drives the real production
  exploration turn on canned results, scoring both "should open a path" and "should stay quiet"
  cases, so a candidate API model can be measured on this one capability for a few API calls.
  Gated OFF by default (`LabConfig.hypothesis_driven` / `BIOAGENT_HYPOTHESIS_DRIVEN`).

- 2026-07-31 · `claude` · (worktree `ziyaoma-pr-merge-status-544fde`, merged PR #28 from
  `<ucinetid>-stack:feat/paperqa-embedding`) — **added** `deploy/paperqa/INTEGRATION_HANDOFF.md`
  (how the deep_literature tool reaches the HPC3 PaperQA index) and
  `skills/literature-corpus-recovery/references/{publisher-access,scripts}.md` (the two reference
  files the corpus-recovery skill's SKILL.md already pointed at). No new source modules: the
  chat-route wiring is content-only in `agents/quick_chat.py` (forced deep_literature grounding for
  literature questions) and `frontend/console/app.js` (plain `[N]` citations, no `#ref` anchors).
  Nothing removed. Conflict resolution for the merge touched tests only — see `7c6a075`.

- 2026-07-27 · `claude` · (worktree `agent-ae5db1eff54a6dd2e`, branch `refactor/drop-lazy-gpu`)
  **renamed** `tests/test_lazy_gpu.py` → `tests/test_connect_provisioning.py` — the lazy GPU path
  it covered is gone (Yijun: it works badly on our cluster and its frontend state machine is
  confusing; prod already ran `BIOAGENT_LAZY_GPU=0`). The file was rewritten rather than deleted:
  it now asserts the opposite invariant — SSH and the GPU come up TOGETHER in one `/api/connect`,
  and no deferred-provisioning entry point exists. Content edits alongside (no structure change):
  `gateway/app.py` (removed `_ensure_gpu_ready_blocking`, `POST /api/connect/gpu`, the
  `conn.alloc is None` triggers in `_run_lab`/`_run_quick_chat`, the SSH-only `connected` status),
  `gateway/settings.py` (removed the `lazy_gpu` field), `frontend/console/app.js` + `styles.css`
  (one status progression: connecting → provisioning → ready), `configs/aiscientist.example.env`,
  `deploy/{analysis,vep,lirical}/*`, `docs/hpc3_offload_migration.md`.

- 2026-07-27 · `claude` · (worktree `agent-ad23b88a3923526cf`, branch `feat/chat-context-compaction`)
  **added** `src/bioagent/agents/chat_context.py` — context awareness + compaction for the FAST
  CHAT path (bounded ~24K prompt budget, rolling model-written summary of older turns, exact
  token counting via an injected counter). Split out of `quick_chat.py` so the knobs
  (`ChatContextLimits`, inherited by `QuickChatConfig`) and the fitting algorithm are testable on
  their own; reuses `research_harness`'s estimator primitives rather than re-deriving them.
  **added** `tests/test_chat_context.py` — offline coverage of the compaction algorithm (no gateway
  import). Edited (not new): `agents/quick_chat.py` (config inheritance + injected
  `count_tokens_fn`/`summarize_fn`/rolling-summary params), `gateway/app.py` (`_run_quick_chat`
  binds both, per-conversation summary memory, `chat_context` WS event), `frontend/console/*`
  (occupancy indicator near the composer), `tests/test_quick_chat.py`.

- 2026-07-24 · `claude` · (worktree `agent-ade858ae0028f24a3`, branch `feat/content-aware-multifile`)
  **added** `reports/2026-07-24/content-aware-multifile.md` (design note), `tests/test_bind_set.py`
  (Phase A: the multi-file bind-set), `tests/test_content_routing.py` (Phase B: content-triage
  overrides suffix routing), and `tests/test_run_start_triage.py` (Phase C: run-start auto-describe).
  All committed. Feature ② (multi-file bind-set) + two approved
  enhancements to feature ①, building on `tools/dataset_inspect.py` (does NOT re-implement peek/describe).
  Edited (not new): `gateway/app.py` — `LabRequest.datasets` (bind-set alongside the legacy
  `dataset_path`), `_select_bound_datasets`/`_stage_secondary_dataset`/`_primary_dataset_record`,
  `_run_lab` multi-file staging + run-start auto-describe (Phase C), `_write_run_state`/`_prepare_continue`/
  `_followup_target` (persist/resume/compare the whole set); `agents/preset_pipelines.py`
  (`select_pipeline` routes on content modality, suffix fallback); `agents/research_lab.py` (threads
  `content_modality` from decisions into `select_pipeline`); `frontend/console/app.js` + `index.html`
  (minimal multi-attach: toggle files into the bind-set, one chip each, post `datasets`).

- 2026-07-24 · `claude` · (worktree `agent-a21f497c151923a22`, branch `feat/file-ingest-agent`)
  **added** `src/bioagent/tools/dataset_inspect.py`, `tests/test_dataset_inspect.py`, and
  `reports/2026-07-24/file-ingest-agent.md` (+ indexed in `reports/README.md`). All committed; nothing
  moved or deleted. Feature ① of the file-ingest line: a GENERAL, LLM-driven "skim any uploaded file
  and get the gist" step that AUGMENTS (does not replace) the suffix-based `_primary_suffix` routing.
  * `tools/dataset_inspect.py` — import-clean without paramiko/gateway (mirrors `tools/hpo_terms/mapper.py`,
    so it unit-tests on a bare checkout; `h5py` is an OPTIONAL import that degrades). Exposes
    `peek_dataset` (deterministic, NEVER-raises bounded-head peek: magic bytes, size, VCF header →
    assembly/samples/caller/bgzip, HDF5 tree via h5py w/o loading matrices, csv/tsv columns, gz head,
    else text/hexdump), `describe_dataset(peek, chat_fn=…)` (LLM triage → structured JSON, deterministic
    facts stamped back over the model so it can't contradict the bytes; falls back deterministically with
    no model), `inspect_dataset`, and `make_inspect_dataset_tool()` (the `inspect_dataset` HarnessTool,
    `think=False` load-bearing).
  * Edited (not new): `agents/registry.py` (registered `make_inspect_dataset_tool` → present in
    `build_scientist_catalog`, NOT in `build_quickchat_catalog`); `gateway/app.py` — `peek_dataset` runs
    synchronously at upload (`/api/upload` single-file + `/api/upload/chunk` finalize) on the still-local
    file BEFORE dfs3b staging (peek on the response + one-line gist in the toast), plus a NEW on-demand
    endpoint `POST /api/dataset/describe` (peek + LLM description, gated on `_vllm_reachable`, reads a
    bounded base64 head for remote dfs3b files, and NEVER provisions a GPU — upload/triage must not
    trigger the lazy A100 spin-up); `tests/test_gateway_lab.py` (+3 tests: peek-on-upload response, the
    describe endpoint's no-model deterministic path, and the path-required 400). Why: Rui — "no matter
    what the file is, the agent should first skim it and get the gist." Multi-file/bind-set is feature ②
    (deliberately untouched here).

- 2026-07-20 · `claude` · (worktree `agent-a42324fa6239790bc`, branch
  `feat/fast-chat-path-and-inline-mermaid`) **added** `src/bioagent/agents/quick_chat.py`,
  `tests/test_quick_chat.py`, `frontend/console/mermaid.min.js`, and
  `reports/2026-07-20/fast-chat-path-and-inline-mermaid.md`. All committed. Nothing moved or deleted.
  * `agents/quick_chat.py` — the **fast path**: an answer-first, streaming, tool-capable ReAct loop
    that is NOT a smaller lab (no PI, no agenda, no Critic, no report bundle). Its own module rather
    than a branch inside `research_lab.py` so it can be tested with zero gateway imports — the local
    env has no `paramiko`, so anything importing `gateway/app.py` cannot run here.
  * `tests/test_quick_chat.py` — 22 offline tests for that loop AND for the new
    `vllm_client.chat_tools_stream` (canned SSE against a stubbed `urlopen`). Deliberately does not
    import `gateway.app`, for the reason above.
  * `frontend/console/mermaid.min.js` — **vendored** mermaid v11.12.0 (2.7 MB, sha256
    `07e37dfa…3c4b`), copied from the npm package inside the locally-installed Antigravity IDE, NOT
    fetched from a CDN: prod has no guaranteed egress and `mmdc` is not installed there, so inline
    chat diagrams must render client-side from a local asset. Sits next to the existing vendored
    `cytoscape.min.js` (same precedent). Loaded LAZILY at runtime — only a message that actually
    contains a ```` ```mermaid ```` fence pays the 2.7 MB.
  * `reports/2026-07-20/…` — the design note (routing, protocol, mermaid sandboxing, and an explicit
    list of what is NOT verified). Indexed in `reports/README.md`.

- 2026-07-17 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **added** `reports/2026-07-17/handoff-to-literature-line.md`
  (+ indexed in `reports/README.md`) — a cross-line handoff for Ziyao's literature line ahead of the
  PaperQA2 integration. Deliberately NOT written into `handoff/ziyao/` because `CLAUDE.md` says each
  line owns its own handoff and must not edit another's; it is a dated report for him to fold in.
  Leads with the empty-completion trap (a bounded call to the served REASONING model returns "" with
  no error once the thinking trace eats `max_tokens` — the exact bug that made map_phenotype_to_hpo
  return 0 terms in prod), since the literature line calls the same endpoint and would hit it blind.

- 2026-07-17 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **added** `reports/2026-07-17/protocol-vs-skill-format-ab.md`,
  **rewrote** `reports/README.md`, **deleted** `reports/2026-06-09/` (Yijun's call — superseded).
  * The A/B report rescues the PROTOCOL-vs-SKILL experiment's numbers, which existed ONLY as
    `raw.json`/`rows.json`/`run*.log` inside `experiments/protocol_format/results/` — a **gitignored**
    dir in the throwaway worktree `silly-diffie-a165f8`. One `git worktree remove` and they were gone.
  * `reports/README.md` now states the folder's purpose (过程报告 / process reports) and carries house
    rules, incl. the new standing one from Yijun: **run artifacts backing a claim must be RETAINED from
    now on** (LIRICAL HTML holds the per-term LR breakdown; those runs kept only the TSV, which is why
    "which term was worth how much" is unanswerable without a re-run — not re-running now, by decision).
  * Deleting `reports/2026-06-09/` would have dangled a **live markdown link** in
    `handoff/yijun/HANDOFF.md` §11; that reference was rewritten in place to note the report was retired
    and to give the exact `git show` command to recover it. Repo re-grepped: no dead links remain.
    (`handoff/yijun/HANDOFF.zh-CN.md` had no such link.)

- 2026-07-17 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **moved** `docs/phenotype_pipeline_validation_report.md` →
  **`reports/2026-07-17/phenotype-pipeline-validation.md`**, and **added `reports/README.md`** (an index
  + the what-belongs-where table + house rules). Yijun's call: dated human-readable write-ups belong in
  `reports/<YYYY-MM-DD>/<slug>.md` (the convention `reports/2026-06-09/` already set), not in `docs/`,
  which stays reference material. Relative links re-pointed two levels up and each one re-verified.
  Also corrected a real error in the report while moving it: the "hand-picked HPO terms" baseline was
  **authored by Claude**, not curated by a clinician, so that comparison is **LLM-vs-LLM** — the report
  now says so explicitly and flags that no clinician-curated baseline exists for these fixtures.

- 2026-07-17 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **added** `docs/phenotype_pipeline_validation_report.md`
  — the validation report for the `phenotype_variant_diagnosis` line. **Why it had to exist:** its
  evidence tables (the real-Qwen → LIRICAL runs: IMPG2 rank 1 @ LR 12.647 on both a synthetic and the
  4.9M-variant WGS; the A/B note-flip on one identical VCF; the 8x posterior swing between hand-picked
  and LLM terms) previously lived ONLY in `sample_data/**` and `full_sample/README.md`, which are
  **gitignored** — so none of it was on main and it would have been lost with the working dir. Now
  consolidated into a tracked doc with HPC3 job ids. Cross-links to the two existing case docs verified.

- 2026-07-17 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **added** `preset_pipelines/phenotype_variant_diagnosis/PROTOCOL.md`
  — the researcher-auditable rendered view of that pipeline's `SKILL.md`. It was the ONLY preset pipeline
  without one (the other 6 already had it); all 7 now do. Includes a **Quick start** section (what to
  attach, a sample note, the exact HPO terms + IMPG2 rank-1 result to expect) per Yijun's request.
  Note on precedence, since the two files look interchangeable: **`SKILL.md` is the only file the code
  loads** (`preset_pipelines.py` globs `*/SKILL.md`; `grep -rn PROTOCOL src/` = zero hits), so PROTOCOL.md
  is a human-facing derivative and is invisible to the model — verified the loader still returns 7
  pipelines with phenotype among them after adding it. Regenerate it if SKILL.md's steps change.

- 2026-07-16 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **added** `preset_pipelines/phenotype_variant_diagnosis/examples/full_sample/`
  — a complete, runnable HPO+VCF sample for HPC3: `case_note.txt` (full synthetic clinical note shaped to
  IMPG2 vitelliform-MD's real HPO annotations + the systemic negatives), `sample_impg2.vcf` (self-contained
  GRCh37, IMPG2 compound-het p.Arg1088*/p.Arg131Cys, 4 PASS + 1 non-PASS), `extracted_hpo.json` (the 4
  observed + 12 excluded terms the real Qwen3.6-35B-A3B pulled from the note), `run_on_hpc3.sh`
  (`sbatch run_on_hpc3.sh [synthetic|wgs]` — end-to-end LIRICAL, prints top 5), `README.md`. Known answer
  is IMPG2 so the run is verifiable. Fixed a SLURM `$0`-vs-`$SLURM_SUBMIT_DIR` path bug in the script
  (caught by running it on HPC3 before shipping).

- 2026-07-15 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **added** `preset_pipelines/phenotype_variant_diagnosis/examples/`
  — a SYNTHETIC end-to-end test case for Rui to run: `demo_case.vcf` (GRCh37, chr-prefixed, 4 PASS + 1
  LowQual) + `case_note_A_stargardt.txt` / `case_note_B_bbs.txt` (English) + `EXPECTED_RESULTS.md`.
  Design: ONE VCF carrying TWO plausible AR candidates (ABCA4 compound-het p.Gly1961Glu + p.Glu531Ter;
  MKKS hom p.Gly52Asp) so the GENOTYPE cannot choose — only the phenotype can. Two notes → the top gene
  must FLIP (A→ABCA4, B→MKKS); same answer on both = the phenotype isn't driving the scoring. Variants
  are real public ClinVar loci verified against Ensembl VEP GRCh37 (the person/notes are invented).
  Also edited `tools/hpo_terms/mapper.py`: new `needs_llm` guard — building this fixture proved the
  LLM-less fallback read a COUSIN's RP and a DENIED night blindness as the patient's own findings
  (actively wrong, not just low recall), so it now refuses on negation/family-history cues; a bare
  diagnosis label still maps. Tests in `tests/test_hpo_mapper.py`.

- 2026-07-15 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **added** `tests/test_case_note.py` — the SECOND attachment
  slot: the patient's clinical description, alongside the VCF. Carried as TEXT on the run request
  (`LabRequest.case_note`), NOT as an upload: a run binds exactly one dataset (that slot must hold the
  VCF), and the note's only consumer (`map_phenotype_to_hpo`) runs in-process on the gateway, never in a
  Slurm container — so it needs no dataset row and no bind. Edited (not new): `gateway/app.py`
  (`case_note` field + `_clean_case_note` + decisions + run_state persistence/resume),
  `tools/hpo_terms/mapper.py` (falls back to the attachment; reports `text_source`),
  `frontend/console/index.html` + `app.js` ("Attach a case note" menu item, chip, FileReader → posted
  with the run), `preset_pipelines/phenotype_variant_diagnosis/SKILL.md`, `docs/free_text_to_hpo_mapping.md`.
  Covers TEXT notes only (.txt/.md); a second DATA file (BED panel, 2nd VCF) still needs the bind-set work.

- 2026-07-15 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **no structural change** — logged for orientation: fixed
  folder-upload dataset detection in `gateway/app.py` so a folder containing a **VCF** resolves
  (`_MATRIX_SUFFIXES` → `_PRIMARY_SUFFIXES` + new shared `_primary_suffix`/`_primary_rank`; `.vcf.gz`
  was missed entirely by last-suffix-only matching). The local + remote finders now share ONE ranking
  (they were duplicate implementations and had drifted). Live prod bug: uploads land on dfs3b
  (`BIOAGENT_UPLOADS_ON_HPC=1`, verified), so folder uploads take the REMOTE branch, which had no
  primary → left `dataset_path` UNSET → the run silently had no dataset. Tests in `tests/test_uploads_hpc.py`.

- 2026-07-15 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **added** `preset_pipelines/phenotype_variant_diagnosis/SKILL.md`
  — the **VCF + case-description** protocol (`data_type: variants`, sibling of `variant_annotation`, which
  stays the VCF-only path). Two independent tracks (annotate_variants ‖ map_phenotype_to_hpo→run_lirical)
  reconciled at the end; runs the phenotype scoring **iff** a clinical description exists (enforced in
  code: no text → no HPO terms → run_lirical errors; the old HP:0000556 default can't fire). Note it says
  the case text comes from the CHAT, not a file: a run binds exactly ONE dataset, so an attached note
  would displace the VCF. Edited (not new): `tools/hpo_terms/index.py` (+`hp_json_release`/`release_date`),
  `tools/phenotype_dx.py` (+`hpo_release_drift` — our lexicon vs LIRICAL's staged hp.json, checked every
  run), `tests/test_preset_compose.py` (+ a check that every pipeline's `tools:` frontmatter names REAL
  catalog tools — nothing resolved them before), `tests/test_hpo_mapper.py`, `docs/free_text_to_hpo_mapping.md`.

- 2026-07-15 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/free-text-hpo-mapping-c61c74`) **added** the free-text→HPO mapper — the missing front end of
  the phenotype line (clinicians write prose, LIRICAL needs HPO IDs):
  **`src/bioagent/tools/hpo_terms/index.py`** (the HPO ontology index = the CLOSED SET: lexical search +
  ID validation), **`src/bioagent/tools/hpo_terms/hpo_lexicon.tsv.gz`** (GENERATED + committed, ~390 KB,
  19,120 current + 577 obsolete terms from HPO 2026-06-23 — committed so the tool runs offline with no
  HPC3/network; regenerate, don't hand-edit), **`src/bioagent/tools/hpo_terms/mapper.py`**
  (the `map_phenotype_to_hpo` tool: LLM extracts phrases + negation → code retrieves real candidates →
  LLM picks a candidate NUMBER → code re-validates, so the model can never author an ID),
  **`scripts/build_hpo_lexicon.py`** (regenerates the lexicon from `hp.json`),
  **`scripts/hpo_mapper_smoke.py`** (real-LLM smoke test — the scripted-LLM unit tests can't cover
  extraction quality), **`tests/test_hpo_mapper.py`** (31 tests), **`docs/free_text_to_hpo_mapping.md`**.
  Edited (not new): `tools/hpo_terms/__init__.py` (word-boundary keyword matching — plain substring let
  `ird` fire inside `third`), `tools/hpo_terms/ird_hpo.tsv` (+3 rows/aliases found missing against the
  lab's real case sheet: choroidal/pattern dystrophy, BBS, RP), `tools/phenotype_dx.py` (`run_lirical`
  now validates every incoming HPO ID against the ontology), `agents/registry.py`, `tests/test_registry.py`,
  `docs/README.md`. Why: Rui Chen — "医生通常不使用HPO术语而是使用自由文本".

- 2026-07-14 · `claude` · (worktree `eyeserver-gpu-request-check-4b9621`, branch
  `claude/lirical-ird-confidence-scoring-99add1`) **added** the LIRICAL phenotype→disease workflow
  (build kit + runner, gated OFF): **`deploy/lirical/`** (`lirical.def` = JRE 17 + LIRICAL v2 CLI baked;
  `build_and_stage.sh` = build sif + `lirical download` data + optional Exomiser DB + smoke test;
  `README.md`), and **`src/bioagent/tools/phenotype_cli.py`** (in-container CLI, the `variant_cli`
  counterpart). Edited (not new): `tools/phenotype_dx.py` (real runner — phenopacket + `prioritize` cmd
  builders + `run_lirical` orchestration), `gateway/settings.py` (`phenotype_on_hpc` + `lirical_*`),
  `tests/test_phenotype_dx.py` (6→14 tests), `docs/phenotype_gene_confidence_rag_spec.md` (status +
  Rui Chen's decisions + the Exomiser-version finding). Why: Rui Chen approved the plan ("方案批准了")
  and asked us to install the LIRICAL workflow. **Then built + verified on HPC3 same day:** `lirical.sif`
  (LIRICAL v2.4.1, Sylabs `--remote`) + LIRICAL data + fresh **Exomiser 2406_hg19** (27.7 GB) staged under
  `/dfs3b/ruic20_lab/software/{bioagent/containers,reference/lirical}`; both modes smoke-tested (RP top in
  phenotype-only; ABCA4 p.G1961E sharpened the differential in genotype-aware). `build_lirical_cmd`
  corrected to the real v2.4.1 `prioritize` CLI. **Then gateway-wired** (`agents/registry.py` tool
  `run_lirical` + routing; `gateway/app.py` phenotype `SlurmAnalysisExecutor` mirroring VEP;
  entrez→symbol reconcile fix) and **ff-merged to main `7c9a8e8`** (pushed to origin, bypassing the PR
  gate per Yijun). Gated OFF; activation = admin sets `.env` + Yijun runs `sync_deploy.sh`. ⚠️ the lab's
  existing Exomiser (`1805_hg19`/`exomiser-cli-10.1.0`, 2018, hg19-only) is too old for LIRICAL v2 (needs
  ≥ 2302) — hence the fresh 2406 DB; phenotype-only needs none.

- 2026-07-14 · `claude+user` · (worktree `vcf-normalization-variants-cef98c`) **moved** 12 superseded docs
  `docs/*.md` → **`docs/archive/`** (biomni_kosmos_integration, hpc3_console, literature_embedding_plan,
  reference_architecture, kosmos_kernel_guardrails, phase2_hpc_compute, project_plan, agent_dashboard,
  harness_rules, answer_framework, minimal_framework, frontend_ux_fixes) + **added** `docs/archive/README.md`.
  Why: keep them only as an early-decision record. ALL referrers repointed to `docs/archive/…` in the same
  change (README(.zh-CN), handoff/yijun + handoff/ziyao, reports/, `docs/agent_registry.yaml`,
  `docs/repository_boundaries.md`, `scripts/pr_review_gate.py` [required_files], code comments in
  `gateway/gpu.py` + `integrations/biomni_runtime.py`). The `docs/diary/` log stays in place. Older
  filemanager entries keep their original `docs/…` paths (they record the state at that date).

- 2026-07-14 · `claude` · (worktree `vcf-normalization-variants-cef98c`) **added** `docs/README.md` — an
  index of every doc with a status tag (Current / Reference / Superseded), fixing the "no map of which
  doc is which" problem. Non-destructive: superseded docs (Biomni, Ollama-console, pre-PaperQA2
  embedding plan, early framing) are tagged, NOT moved/deleted (each still referenced by 1–6 files).

- 2026-07-14 · `claude` · (worktree `vcf-normalization-variants-cef98c`) **added** the phenotype→disease
  differential-diagnosis line (scaffold, inert / not wired): `src/bioagent/tools/phenotype_dx.py` (LIRICAL
  TSV parser + two-track reconciliation + PaperQA2 placeholder), `tests/test_phenotype_dx.py`,
  `docs/phenotype_gene_confidence_rag_spec.md` (rewritten to disease-level: LIRICAL primary + PaperQA2
  evidence track, currencies not blended), and `docs/paperqa2_evidence_layer_contract.md` (interface
  handoff for the classmate's PaperQA2 evidence track). Why: Rui Chen's "symptom+gene→per-disease
  confidence" ask. LIRICAL not yet staged on HPC3 (`run_lirical` gated); PaperQA2 is a placeholder.

- 2026-07-13 · `claude+user` · (worktree `vcf-normalization-variants-cef98c`) **added**
  `preset_pipelines/variant_annotation/PROTOCOL.md` — the operon-style, researcher-auditable protocol for
  the VCF/IRD pipeline, co-located next to its `SKILL.md` (the preset loader reads ONLY `SKILL.md`, so this
  is inert to execution). Current: folds in the IRD-parity layers now on main (pre-VEP panel, HGMD/retina/
  ATAC annotation layers, disease-model tiering, the verified 99 s result). Companion PDF + raw `.md` for
  advisor reporting were rendered to `~/Downloads/VCF_Pipeline_Protocol.{pdf,md}` (NOT committed — personal).
  Completes the set: every `preset_pipelines/*/` now carries a `PROTOCOL.md` alongside its `SKILL.md`.

- 2026-07-13 · `claude` · (worktree `vcf-normalization-variants-cef98c`) **added**
  `preset_pipelines/scgpt_annotation/PROTOCOL.md`. Human-auditable operon-style protocol for the scGPT
  foundation-model per-cell annotation preset, mirroring the
  `experiments/protocol_format/variant_annotation/PROTOCOL.md` exemplar (per-step `<details>`, 🔬
  agent-chosen vs ⚙️ fixed legend, ✅ verify blocks, dual-use transfer-AND-cross-validate box, ⚑
  merge-by-barcode caveat). Documentation-only, rendered strictly from that pipeline's `SKILL.md` (6
  executable steps) — no code changed.

- 2026-07-13 · `claude` · (worktree `vcf-normalization-variants-cef98c`) **added**
  `preset_pipelines/perturbation_analysis/PROTOCOL.md`. Human-auditable operon-style protocol for the
  pooled-perturbation / Perturb-seq preset, mirroring the
  `experiments/protocol_format/variant_annotation/PROTOCOL.md` exemplar (per-step `<details>`, 🔬
  agent-chosen vs ⚙️ fixed legend, ✅ verify blocks, E-distance/control strategy box). Documentation-only,
  rendered strictly from that pipeline's `SKILL.md` (9 steps) — no code changed.

- 2026-07-13 · `claude` · (worktree `vcf-normalization-variants-cef98c`) **added**
  `preset_pipelines/celltype_annotation/PROTOCOL.md`. Human-auditable operon-style protocol for the
  single-cell cell-type annotation preset, mirroring the
  `experiments/protocol_format/variant_annotation/PROTOCOL.md` exemplar (per-step `<details>`, 🔬
  agent-chosen vs ⚙️ fixed legend, ✅ verify blocks). Documentation-only, rendered strictly from that
  pipeline's `SKILL.md` (6 steps) — no code changed.

- 2026-07-13 · `claude` · (worktree `vcf-normalization-variants-cef98c`) **added**
  `preset_pipelines/differential_expression/PROTOCOL.md`. Human-auditable operon-style protocol for the
  differential-expression preset (6 steps: QC → define comparison → per-cell-type DE → enrichment →
  cross-cell-type synthesis → figures), mirroring the
  `experiments/protocol_format/variant_annotation/PROTOCOL.md` exemplar (per-step `<details>`, 🔬
  agent-chosen vs ⚙️ fixed legend, ✅ verify blocks). Documentation-only, rendered strictly from that
  pipeline's `SKILL.md` — no code changed.

- 2026-07-13 · `claude` · (worktree `vcf-normalization-variants-cef98c`) **added**
  `preset_pipelines/gene_signature_scoring/PROTOCOL.md`. Human-auditable operon-style protocol for the
  gene-signature-scoring preset, mirroring the `experiments/protocol_format/variant_annotation/PROTOCOL.md`
  exemplar (per-step `<details>`, 🔬 agent-chosen vs ⚙️ fixed legend, ✅ verify blocks). Documentation-only,
  rendered strictly from that pipeline's `SKILL.md` — no code changed.

- 2026-07-13 · `claude` · (worktree `silly-diffie-a165f8`) **added** `experiments/protocol_format/`
  (`README.md`, `variant_annotation/PROTOCOL.md`, `ab_test.py`, `results/` — outputs gitignored).
  Throwaway prototype of an operon-style, researcher-auditable protocol format for the variant pipeline
  + an OpenRouter A/B harness. **Intentionally OUTSIDE `preset_pipelines/`** so the preset loader does
  NOT pick it up — the stable pipeline is untouched. Same change-set (content edits, not structural):
  `settings.py` `vep_assembly` fallback GRCh38→**GRCh37** (header auto-detection still wins; 37 is the
  right fallback for an eye lab) + de-misleading the "GRCh38 by default" prose in the stable
  `variant_annotation/SKILL.md`.

- 2026-07-12 · `claude` · **added** `src/bioagent/tools/hpo_terms/` (`ird_hpo.tsv` 15 verified IRD phenotype→HPO terms + `__init__.py` inferer) + `tests/test_hpo_terms.py`. Upstream-agent HPO inference for Exomiser, NO human-in-the-loop, default HP:0000556. Branch `feat/ird-parity`.

- 2026-07-12 · `claude` · **added** `src/bioagent/tools/ird_annotate.py` (IRD annotation layers — HGMD 15bp/MATCH, retina-exon, ATAC, dbscSNV ada/rf + reason_for_inclusion cascade + tabix batch runner, all pure/injectable) + `tests/test_ird_annotate.py`. Wired (gated off) into vcf_offline/variant_cli/settings/gateway. Branch `feat/ird-parity`.

- 2026-07-12 · `claude` · **added** `src/bioagent/tools/ird_prioritize.py` (disease-model gene-level
  tiering — dominant ≤1e-4 / recessive ≥2 ≤5e-3 / X, pure logic per the spec) + `tests/test_ird_prioritize.py`.
  Branch `feat/ird-parity` (IRD-parity Phase 2 core).
- 2026-07-12 · `claude` · **added** `docs/ird_filter_spec.md` — extracted spec of the lab's annotate/filter/prioritize logic (exact freq/splice cutoffs + reason_for_inclusion cascade), the port target for the IRD line. Branch `feat/ird-parity`.

- 2026-07-12 · `claude` · **added** `src/bioagent/tools/gene_panels/` (`__init__.py` loader +
  `ird_retnet.txt`, 258-gene IRD panel copied from the lab's RetNet list) + `tests/test_gene_panels.py`.
  On branch `feat/ird-parity` (IRD-parity Phase 1 layer 1 — known-gene panel). NOT on main yet.
- 2026-07-12 · `claude` · **added** `docs/ird_pipeline_parity_roadmap.md` — the phased roadmap ("path")
  to bring the variant line up to parity with the lab's IRD reference pipeline (11 layers → Phase 0
  deploy / Phase 1 turn-on-built / Phase 2 disease-model logic / Phase 3 external-data). Committed to main.

- 2026-07-11 · `claude` · (worktree `strange-turing-972fb6`) **ADD** `tests/test_env_aliases.py` — covers the
  brand env-var migration compat layer. Content edit same change-set: `src/bioagent/core/config.py` gains
  `apply_brand_env_aliases()` (mirror `BIOAGENT_*` ⇄ `AISCIENTIST_*` at `.env` load, new-brand wins) +
  `env(name)` helper; `load_project_env` calls it. Zero-downtime phase-1 of the BioAgent→AiScientist
  rename so ops can set either env prefix. Why: Yijun's rebrand (see memory `project-renamed-aiscientist`).

- 2026-07-11 · `claude` · (worktree `strange-turing-972fb6`) brand rename **content edits** (no new files):
  user-facing `BioAgent`→`AiScientist` across 58 files (email subject/body in `auth_routes.py` + default
  From in `email_send.py`; app.py display strings; README EN+zh; docs; deploy comments). KEPT infra:
  `bioagent` package/`BIOAGENT_*`/`/data/BioAgent`/service/SSH/`BioAgentPrototype` (proven unchanged).

- 2026-07-10 · `claude` · (worktree `strange-turing-972fb6`) **ADD** `scripts/backfill_run_conversation_id.py`
  — one-off, DRY-RUN-by-default backfill of `runs.conversation_id` for pre-migration runs: recovers the
  `run_id → conversation.id` link from chat history (`messages.meta` bundle/artifact URLs), fills only
  NULL rows, skips run_ids referenced by >1 conversation (historical leak). Run on the server as
  `bioagent` with `--commit`. Audit-value now (the column isn't read for routing yet).

- 2026-07-10 · `claude` · (worktree `strange-turing-972fb6`) **ADD** `tests/test_run_isolation.py` — offline
  tests for the run/conversation ISOLATION fix (event tagging with run_id+conversation_id; per-run
  cancel/plan events + `resolve_run` targeting; per-conversation fresh-vs-replan; no report on a
  cancelled/empty run, incl. an end-to-end `_run_lab` drive). Content edits in the same change-set (not
  structural): `gateway/{app.py,models.py,auth_routes.py,db.py}` (RunState + `Connection.runs`/`active_run`/
  `last_run_by_conversation` + proxies + `_tag`/`begin_run`/`bind_run_id`/`resolve_run`; `conversation_id`
  on the run request models + Run model + `record_run_start` + an idempotent ADD COLUMN migration in
  `init_db`; skip-report-on-cancel), `frontend/console/app.js` (demux WS events by conversation_id),
  `agents/{preset_pipelines.py,research_lab.py}` + the 6 `preset_pipelines/*/SKILL.md` (`data_type`
  modality + `drop_conflicting_pinned` so a VCF's auto pick drops a pinned scanpy pipeline). Why: fix the
  gateway isolation bug (memory `run-conversation-isolation-bug`).

- 2026-07-08 · `claude` · (worktree `silly-diffie-a165f8`) **ADD** `deploy/vep/stage_annotation_dbs.sh`
  — idempotent staging (Jin Li's download-once + check-existence pattern) for the VEP predictor DBs
  (AlphaMissense/CADD/REVEL/ref FASTA) into the shared plugins dir. Run on HPC3; deploy is Yijun's.
  (Same commit wires Rui Chen's IRD known-gene + AF filter into the variant tool/preset — content edits.)

- 2026-07-08 · `claude` · (worktree `silly-diffie-a165f8`) **ADD** `deploy/vep/PREDICTOR_STAGING.md` —
  English install/stage checklist for the VCF path (predictor plugins + reference FASTA + TileDB dep),
  with a "is local staging necessary?" justification. For Yijun to forward to Jin Li for review. Also
  corrected several stale "compute nodes have no network" doc/comments across `src/` + `deploy/` to
  reflect the verified on-demand-networking reality (gpu.py, settings.py, genesets README, vep/scgpt/
  vlreview deploy docs).

- 2026-07-08 · `claude` · (worktree `silly-diffie-a165f8`) **ADD** `docs/vcf_pipeline_tools.md` — the
  tool + reference-data inventory for the VCF path (VEP/ClinVar/gnomAD/bcftools/CADD/AlphaMissense/
  REVEL/SpliceAI/TileDB-VCF: what each is, code location, size, live/staging/deferred status, env
  vars). Cross-linked from `preset_pipelines/variant_annotation/SKILL.md`. Why: Yijun asked for a doc
  of the tools used in VCF processing. (Predictor data itself staged on HPC3 dfs3b, not in the repo.)

- 2026-07-08 · `claude` · (worktree `silly-diffie-a165f8`) **ADD** 3 operon-derived variant skills:
  `skills/normalize_vcf/`, `skills/vcf_qc_stats/`, `skills/clinical_variant_prioritization/` (each
  `SKILL.md` + `reference.py`) + `tests/test_operon_variant_skills.py`. Ported from
  github.com/swaruplab/operon `variant-calling-*` protocols (bcftools norm; VCF QC stats; ACMG-lite
  triage tiering) into our folder form; wired as optional steps into
  `preset_pipelines/variant_annotation/SKILL.md`. Why: Yijun asked to integrate operon's VCF approach.

- 2026-07-08 · `claude` · (worktree `silly-diffie-a165f8`) **MOVE+ADD** atomic skills flat `.py` →
  folder form. `git mv skills/<name>.py → skills/<name>/reference.py` (10 skills, history preserved)
  and authored `skills/<name>/SKILL.md` (frontmatter `name`+`description` + `## When to use`
  guidance) for each; added `skills/README.md`. Why: match the Anthropic Skill definition (separate
  description + demonstration) per Yijun. `agents/skills.py` loader now globs `skills/*/SKILL.md`;
  `read_skill_reference(name[, file])` is three-level (manifest → guidance → code). Skill names no
  longer carry `.py` (legacy `.py` refs still resolve via tolerant lookup).

- 2026-07-07 · `claude` · (branch `claude/silly-diffie-a165f8`) **ADD** `skills/variant_output_tables.py`
  + `tests/test_variant_output_tables_skill.py` — a NEW atomic skill (stdlib-only CodeAct template) that
  writes the five standard variant-annotation result tables + summary JSON from the persisted
  `tables/variant_annotation.tsv`, so the orchestrator stops hand-writing (and botching) that
  CSV-dumping/summary-dict run_code. `preset_pipelines/variant_annotation/SKILL.md` edited to point the
  post-processing step at it. Auto-discovered by `agents/skills.py` (flat `skills/*.py`). Merged to main.

- 2026-07-07 · `claude` · (branch `feat/console-ui-polish`) **ADD** `frontend/console/assets/material-symbols/`
  (`material-symbols-rounded.woff2` + `README.md`) — the Material Symbols icon font bundled LOCALLY (the
  icon "素材库"), dropping the Google Fonts CDN so the console renders icons offline / under CSP. Content
  edits alongside: `frontend/console/{app.js,index.html,styles.css}` (local @font-face, emoji→Material
  Symbols, rotating-chevron + smooth-reveal on collapsibles, rAF-coalesced streaming + Claude-style caret,
  sticky non-yanking autoscroll, richer renderMarkdown; liquid-glass panels; refined composer controls;
  `.chat-panel` overflow:visible so the "+ Data" dropdown is not clipped) and `gateway/app.py` (static
  route serves subdirs + caches fonts). Also uses gitignored `frontend/console/serve.py` + `.claude/launch.json`.

- 2026-07-07 · `claude` · **ADD** `src/bioagent/tools/vcf_offline.py`, `src/bioagent/tools/variant_cli.py`,
  `deploy/vep/` (`vep.def`, `build_and_stage.sh`, `README.md`), `tests/test_vcf_offline.py` — new OFFLINE
  VCF variant-annotation line (bcftools + `vep --offline --cache --fork` on HPC3) so WGS-size VCFs no
  longer hit the REST tool's whole-file-in-memory read / 500-cap / rate-limit walls. Also edited
  `variant_annotation.py` (extracted `annotate_variants_rest`), `slurm_analysis.py` (+`extra_ro_binds`
  /`inject_args`/`job_prefix`), `registry.py` (+`variant_executor` routing), `settings.py` (+`VEP_*` /
  `variant_on_hpc`), `gateway/app.py` (variant-executor construction). Worktree `feat/vcf-offline-annotation`.

- **2026-07-07 · claude · ADD** `docs/pi_critic_meeting_protocol.md`, `tests/test_step_meetings.py` —
  PI↔Critic two-way step-meeting protocol (pre-flight necessity/reasonableness gate + post-step
  contribution review that prunes moot steps). Off by default (`LabConfig.step_meetings`); fully
  enacted on the linear planner, amend+floor only on DAG. Code lives in `agents/research_lab.py`.
  On worktree `elastic-chatelet-6c5d2b`, not committed.

- 2026-07-07 · `claude` · (worktree `youthful-sanderson-0131d3`) **Phase 2+3 of the skills/pipelines
  restructure** — the atomic-skill layer + progressive disclosure (per
  `docs/skills_and_pipelines_architecture.md`; 527 green). **added** flat `skills/` atomic-skill
  library (9 `*.py` templates, PROMOTED from the pipelines' `scripts/` via `git mv`:
  annotate_clusters_by_markers, build_variant_db_tiledbvcf, condition_by_celltype,
  crossvalidate_scgpt_vs_leiden, mixscape_escape_filter, pairwise_de, perturbation_de_vs_control,
  perturbation_edistance, score_signature) + **added** `src/bioagent/agents/skills.py` (NEW atomic
  loader: `Skill`/`SKILLS`/`skill_manifest`/`make_skill_reference_tool`). **removed** the per-pipeline
  `scripts/` dirs (now empty). `$BIOAGENT_SKILLS_DIR` now = the atomic library. Content edits:
  `agents/preset_pipelines.py` (dropped `SkillScript`/`scripts` field/`_load_scripts`),
  `agents/research_lab.py` (brief manifest + reference tool now read the GLOBAL atomic library),
  `presets.py` shim, tests. The registry (`agents/registry.py`) is unchanged — the fixed core.

- 2026-07-07 · `claude` · (worktree `youthful-sanderson-0131d3`) **Phase 1 of the skills/pipelines
  restructure** (per `docs/skills_and_pipelines_architecture.md`; behaviour-preserving, 527 green).
  **moved** `skills/` → `preset_pipelines/` (6 folders) and **renamed** `src/bioagent/agents/skills.py`
  → `agents/preset_pipelines.py` (loader now uses pipeline vocab: `PresetPipeline`/`PIPELINES`/
  `get_pipeline`/`list_pipelines`/`select_pipeline`/`compose_pipeline_prompts`; env
  `BIOAGENT_PIPELINES_DIR` with `BIOAGENT_SKILLS_DIR` fallback). `agents/presets.py` shim repointed;
  `research_lab.py` + 3 tests updated. This FREES the `skills/` name for the NEW atomic-skill layer
  (Phase 2). NOT yet merged to main — holding until the full restructure is complete.


- 2026-07-07 · `claude` · (worktree `youthful-sanderson-0131d3`) **Q2 skill-subsystem decouple**
  (behaviour-preserving; 527 tests green). **added** `src/bioagent/agents/skills.py` — the canonical
  skill engine: data model (`Skill`/`SkillScript`), loading (`SKILLS`/`get_skill`/`list_skills`),
  dataset-aware routing (`select_skill`), prompt composition (`compose_skill_prompts`), and the
  progressive-disclosure `read_skill_reference` tool (`make_skill_reference_tool`). `agents/presets.py`
  **slimmed to a re-export shim** (frontend-facing "preset" view: `PRESETS`/`get_preset`/`list_presets`/
  `ResearchPreset` now alias the `skills.py` surface). Content edits: `agents/research_lab.py` (dropped
  `_select_skill`/`_make_skill_reference_tool`/`_compose_skill_prompts`/`_parse_skill_choice`/
  `_SKILL_SELECT_SYSTEM` — now imported from `skills`), `tests/test_preset_compose.py` +
  `tests/test_research_lab.py` (repointed to `bioagent.agents.skills`). This is the seam for future
  skill induction.

- 2026-07-06 · `claude` · **added** `skills/variant_annotation/examples/` (`demo_variants.vcf` +
  `README.md`) — a tiny 8-variant GRCh38 demo VCF (well-known ClinVar variants: HBB/F5/SERPINA1/HFE/
  PAH pathogenic + TP53/MTHFR/PPARG common) for demoing the variant_annotation skill; coords from
  Ensembl, validated end-to-end against live VEP (6 pathogenic, high_priority=6).

- 2026-07-06 · `claude` · (branch `feat/console-ux-and-continuation`) console UX + continuation batch.
  **added** `tests/test_preset_compose.py` (the multi-select skill composer). Content edits (not new
  files): `gateway/app.py` (follow-up plan-mode fix + early `last_run_id` + `presets` field +
  `_compose_preset_prompt` + non-fatal manuscript render + dag default), `gateway/slurm_report.py`
  (render never throws → local fallback), `frontend/console/{index.html,app.js,styles.css}` (DAG default
  no toggle; searchable multi-select skill picker; no dataset auto-attach), `tests/test_slurm_report.py`
  + `tests/test_followup_router.py` (regressions).

- 2026-07-06 · `claude` · (branch `feat/perturbseq-skill`) **added** the Perturb-seq workflow (#3):
  `skills/perturbation_analysis/` (`SKILL.md` + `scripts/perturbation_edistance.py`,
  `perturbation_de_vs_control.py`, `mixscape_escape_filter.py` + `references/methods.md`) and
  `tests/test_perturbation_skill.py`. First pooled-CRISPR skill; pure skill-layer (no `src` change).
  Adapted from k-dense-ai/scientific-agent-skills + scPerturb/pertpy.

- 2026-07-06 · `claude` · (branch `feat/research-skills`) **added** the variant-annotation workflow
  (#4): `src/bioagent/tools/variant_annotation.py` (the `annotate_variants` tool — Ensembl VEP REST +
  ClinVar; registered in `agents/registry.py`), `skills/variant_annotation/` (`SKILL.md` +
  `scripts/build_variant_db_tiledbvcf.py` + `references/apis.md`), and `tests/test_variant_annotation.py`.
  First genomics (VCF) skill; adapted from k-dense-ai/scientific-agent-skills.

- 2026-07-06 · `claude` · **added** `tests/test_capability_log.py` — covers the always-on per-run
  optional-GPU-capability record (`_write_capability_log`/`_scan_tool_invocation` in `gateway/app.py`):
  scGPT + VL review invoked-or-not is now always written to `process/capabilities.log` + `event_log.txt`,
  and scGPT job logs are captured to `process/scgpt_job.log` (via `scgpt_runner`).

- 2026-07-06 · `claude` · **added** `deploy/ACTIVATE_scgpt_vl.md` — server-specific runbook to turn on
  the scGPT and VL-render-review sifs on the deployed eyeserver (exact `.env` lines + verify steps).
  Written after confirming via `eyeserver-admin` that both are OFF/unconfigured in prod `.env`.

- 2026-07-06 · `claude` · **added** `scripts/no_contrast_enrichment_openrouter.py` — real-LLM
  (OpenRouter/Qwen3.6) proof that the no-contrast enrichment guard drops pathway enrichment on an
  already-annotated single-sample dataset and stays inactive when a real contrast exists. Also
  **deleted** local+remote branch `feat/dag-planner` (fully merged into `main`; `main` is 0.2.0).

- 2026-07-06 · `claude` · **merged** `feat/dag-planner` (0.2.0 DAG) and `fix/vllm-tunnel-resilience`
  into `main` — `main` is now the single 0.2.0 mainline; the 0.1.0 pipeline lives on as the frozen
  `v0.1.0` tag (rollback-only, no longer maintained). Resolved one code conflict in `gateway/app.py`
  (kept BOTH main's planner budgets `max_steps/max_rounds` and the DAG planner config).

- 2026-07-05 · `claude` · **added** `tests/test_vllm_recovery.py` on branch `fix/vllm-tunnel-resilience`
  (off `feat/dag-planner`) — pins the mid-run vLLM tunnel/serve auto-recovery (`_heal_vllm_session` +
  `_lab_llm` retry) and the new `BIOAGENT_SLURM_CONSTRAINT` sbatch plumbing. Content edits in this
  change-set (not new files): `gateway/{errors,vllm_client,ssh_gateway,app,settings,gpu}.py`.

- 2026-07-05 · `claude` · **added** `docs/BACKLOG.md` on branch `feat/dag-planner` — deferred large
  initiatives (headline: bring-your-own external API to replace the HPC3 backend; own branch when
  picked up). Also this change-set (content edits, not new files): `pyproject.toml` version 0.1.0→0.2.0,
  `README.md` full product-intro rewrite for 0.2.0, `handoff/yijun/HANDOFF.md`(+zh-CN) 2026-07-05 section
  (release model / rollback / vLLM fix / literature-conflict map). Tag `v0.1.0` created on `main`.

- 2026-07-03 · `claude` · **added** `src/bioagent/agents/agent_memory.py`, `tests/test_agent_memory.py`,
  `scripts/dag_memory_openrouter.py` on branch `feat/dag-planner` — Axis C per-agent evolving memory
  (v1): disk-backed per-agent episodes+lessons, read-before-act / write-after / reflect-at-end, wired
  into `ResearchLab._run_one_node`. Flag-gated (`LabConfig.agent_memory` / env `BIOAGENT_AGENT_MEMORY`,
  DAG only). Memory root = per-owner `conn.workspace/_agent_memory` (eyeserver, outside run_ids).
  Content edits: `research_lab.py`, `gateway/app.py`, `tests/test_research_lab.py`,
  `tests/test_lab_progress_stream.py`. Validated incl. real OpenRouter cross-run learning.

- 2026-07-03 · `claude` · **added** `docs/agent_memory_design.md` on branch `feat/dag-planner` — DESIGN (now v1 implemented): per-agent isolated + evolving memory (Axis C), prioritised over dynamic re-planning; fits on 1× A100 (memory is CPU/disk, ~0 VRAM).

- 2026-07-03 · `claude` · **extended** `docs/dag_planner_design.md` on branch `feat/dag-planner` —
  §6.2 status (roadmap §1–4 DONE), §7 execution closed-loop (Mermaid two-loop state machine +
  invariants), §8 dynamic re-planning DESIGN (frozen/mutable boundary contract). Design only, no code.

- 2026-07-03 · `claude` · **added** `scripts/dag_full_sim_openrouter.py` on branch `feat/dag-planner` —
  FULL end-to-end simulation: every LLM role (PI/structure/coordinator/scientist-tool-calling/critic/
  synthesize) on OpenRouter/Qwen3.6, REAL scanpy tools locally + real Europe PMC; enrichment labeled
  STUB(local-dep: gseapy). Not a CI test (network + heavy). Passed: converged, HITL fired, report real.

- 2026-07-03 · `claude` · **added** `scripts/dag_smoke_openrouter.py` on branch `feat/dag-planner` —
  real-LLM (OpenRouter/Qwen3.6) smoke test for the DAG structure pass + Coordinator; validated the
  branch `s1→s2→s3→{enrichment, literature}` and a correct coordinator pick. Not a CI test (network).

- 2026-07-03 · `claude` · **added** `src/bioagent/agents/dag.py` + `tests/test_dag.py` on branch
  `feat/dag-planner` — DAG plan model (TaskNode/LabPlan, parse/lift/ready-set/cycle-check) for the
  ready-set scheduler. Consumed by `ResearchLab._run_dag` (gated on `LabConfig.planner="dag"`).

- 2026-07-03 · `claude` · **added** `docs/dag_planner_design.md` on branch `feat/dag-planner` —
  design for evolving the linear `_run_loop` into a dependency-DAG + agent self-scheduling + HITL
  decision points + real multi-agent. Design only; awaiting sign-off before implementation.

- 2026-07-03 · `claude` · **added** `scripts/fetch_genesets.py` + `src/bioagent/tools/genesets/`
  (README committed; `*.gmt` gitignored). Downloads GMT gene-set libraries so `run_enrichment` does
  OFFLINE ORA against local files instead of the Enrichr web API — the analysis Slurm container is
  network-off, so the API path could never succeed. Content change alongside: `tools/scrna_pack.py`
  (`run_enrichment` rewritten offline). Run the script once → dfs3b source `genesets/` (or set
  `BIOAGENT_GENESETS_DIR`).

- 2026-07-03 · `claude` · **added** `tests/test_report_regenerate.py` (branch
  `feat/report-regenerate-and-session-persist`) — offline tests for the A1 report-regenerate path
  (`POST /api/report/regenerate`: rebuild a prior run's report from its bundle without re-running
  the PI/analysis). Content changes alongside: `gateway/app.py` (endpoint + `_regenerate_report`),
  `frontend/console/app.js` + `styles.css` (regenerate button + dataset/last-run localStorage
  persistence + connect-form overflow fix). Committed.

- 2026-07-02 · `claude` · **added** `src/bioagent/gateway/slurm_report.py`, `deploy/report/`
  (`report.def`, `build_and_stage.sh`, `.gitignore`), `tests/test_slurm_report.py` (branch
  `feat/hpc3-offload`) — Phase 5 of the HPC3 offload: the report render (pandoc/xelatex) runs as a
  CPU Slurm job on HPC3 so texlive stays off the eyeserver. `SlurmReportRenderer` implements
  report.py's `(cmd,cwd,out,timeout)->(ok,err)` contract (tars the bundle to dfs3b, runs the exact
  pandoc cmd in a deps-only pandoc/texlive image, pulls the PDF/DOCX back); gated by
  `BIOAGENT_REPORT_ON_HPC` with a local-pandoc fallback. Content edits: `tools/report.py`
  (`build_pdf_report(render_fn=...)` + skip the local pandoc check when a renderer is injected),
  `gateway/app.py` (build+inject the renderer), `gateway/settings.py` (`report_on_hpc`,
  `report_image`). `report.def` is `FROM pandoc/extra` (deps-only → `--remote`-buildable).

- 2026-07-02 · `claude` · **changed** Phase 4 from BAKE to BIND (branch `feat/hpc3-offload`, content
  edits only). The bioagent tools are no longer baked into `analysis.sif` (`%files` removed — it
  can't ship local source to a `--remote` Sylabs build anyway, and every tool edit forced a
  rebuild). Instead the gateway tars the live `src/bioagent` and pushes it to `<lab_storage>/<user>/
  pysrc` on dfs3b (`app._sync_bioagent_source_to_hpc`, cached per session), and
  `SlurmAnalysisExecutor` bind-mounts it read-only + sets `PYTHONPATH`. Result: editing a tool needs
  only a normal code deploy, the image is deps-only (rebuilt only when deps change), and `--remote`
  builds work. `analysis.def` + `build_and_stage.sh` reverted to deps-only. Tests updated (335 pass).

- 2026-07-02 · `claude` · **added** `src/bioagent/tools/scrna_cli.py`, `src/bioagent/gateway/slurm_analysis.py`,
  `tests/test_scrna_cli.py`, `tests/test_slurm_analysis.py` (branch `feat/hpc3-offload`) — Phase 4 of the
  HPC3 offload: the scanpy analysis line (QC/clustering/DE/enrichment + preflight) runs as CPU Slurm
  batch jobs on HPC3, gated by `BIOAGENT_ANALYSIS_ON_HPC`. `scrna_cli` is the in-container entrypoint
  (imports the SAME scrna_pack tools; emits a `BIOAGENT_RESULT_JSON` line). `SlurmAnalysisExecutor`
  mirrors `SlurmCodeExecutor` (stage args → sbatch in analysis.sif reading dfs3b in place → parse result
  → sync artifacts back), with an in-process fallback. Content edits: `agents/registry.py`
  (`build_scientist_catalog(..., analysis_executor=)` routes the four real analysis tools),
  `gateway/app.py` (`_run_lab` builds+injects the executor; dataset used in place on dfs3b or staged up),
  `gateway/settings.py` (`analysis_on_hpc`). Needs an `analysis.sif` that can import bioagent (image
  rebuild = ops step); off/mock/unavailable → tools stay in-process unchanged.

- 2026-07-02 · `claude` · **added** `tests/test_uploads_hpc.py` (branch `feat/hpc3-offload`) — Phase 2
  of the HPC3 offload: uploaded datasets stream to HPC3 dfs3b (`<lab_storage>/<user>/uploads`) instead
  of the eyeserver, gated by `BIOAGENT_UPLOADS_ON_HPC`. Content edits in `gateway/app.py` (helpers
  `_hpc_uploads_dir`/`_uploads_on_hpc`/`_is_remote_dataset`/`_stage_upload_to_hpc`/`_ensure_local_dataset`/
  `_active_conn_for_user`; single-file + chunked upload push to dfs3b; `_run_lab` stages remote
  datasets back for the still-local tools; remote-aware `datasets/delete`) and `gateway/settings.py`
  (`uploads_on_hpc`). Folder uploads + preflight-on-HPC3 are the next increment.

- 2026-07-02 · `claude` · **added** `tests/test_lazy_gpu.py` (branch `feat/hpc3-offload`) — Phase 1 of
  the HPC3 offload: covers the SSH/GPU decoupling (SSH-only → status `connected`; GPU provisioned
  lazily + idempotently) and the `BIOAGENT_LAZY_GPU` flag. Content edits alongside: `gateway/app.py`
  (`_provision_blocking` split into `_ssh_connect_blocking` + `_provision_gpu_blocking`, new
  `_ensure_gpu_ready_blocking`, `/api/connect/gpu` endpoint, lazy trigger in `_run_lab`),
  `gateway/settings.py` (`lazy_gpu` field). No frontend wiring yet.

- 2026-07-02 · `claude` · **added** `docs/hpc3_offload_migration.md` (branch `feat/hpc3-offload`) —
  broader migration plan: uploads land on HPC3 dfs3b (not eyeserver) + all srun-able CPU/GPU tasks
  run on HPC3, so eyeserver is a pure gateway. Grounded on the verified fact that `/dfs3b` is NOT
  mounted on eyeserver (SSH-only), which couples upload-to-HPC3 with moving the data consumers to
  HPC3. Phased (SSH/GPU decouple → uploads→dfs3b → preflight → analysis → report render). No code yet.
- 2026-07-02 · `claude` · **added** `src/bioagent/gateway/ssh_credentials.py` +
  `tests/test_ssh_credentials.py` (branch `feat/ssh-key-login`) — SSH-key login: generate an
  Ed25519 keypair, deploy the PUBLIC key to the user's HPC3 `~/.ssh/authorized_keys` over the
  just-authenticated session, store the private key under `<BIOAGENT_STATE_DIR>/ssh_creds/<owner>/`
  (0600, optional passphrase), and reuse it next login (skip password + Duo). Wired into
  `gateway/app.py` (ConnectRequest gains `duo_method`/`credential_id`/`create_key`; new
  `GET/DELETE /api/ssh-credentials`) + the console login form. Duo now defaults to PUSH (the
  6-digit passcode box is removed).

- 2026-07-02 · `claude` · **added** `src/bioagent/gateway/job_store.py`,
  `tests/test_job_store.py`, `tests/test_slurm_reattach.py` (branch
  `fix/slurm-job-persistence-reattach`, merged to `main`). Durable Slurm-job registry +
  reattach layer so a gateway restart mid-analysis leaves a reattachable record instead of an
  orphaned job. Same change-set (content edits, not structural): `gateway/slurm_job.py`
  (on_submit hook, `supervise_job`/`reattach_job`/`resume_incomplete`),
  `gateway/slurm_sandbox.py` (optional `job_store`), `gateway/app.py` (wire store + reconnect sweep).

- 2026-07-02 · `claude` · **added** `docs/analysis_slurm_offload.md` (branch
  `feat/scanpy-slurm-offload`) — research/design doc for moving the scanpy analysis line
  (`scrna_pack.py`: QC/cluster/DE/enrichment) off eyeserver in-process execution and onto HPC3 as
  Slurm batch jobs. Maps what already exists (`slurm_job.py` engine, `analysis.sif`,
  `SlurmCodeExecutor`/scGPT/vlreview patterns) vs the gap (container CLI entrypoint +
  `SlurmAnalysisExecutor` wrapper + `BIOAGENT_ANALYSIS_ON_HPC` switch + dataset staging). No code yet.

- 2026-07-02 · `claude` · **deleted** `deploy/vlreview/build_and_stage.sh` (superseded by
  `scripts/hpc3_vlreview_setup.sh`, which fits RCIC conventions: compute-node build, cache off
  `$HOME`, typed-gres probe, `--remote` fallback since HPC3 has no fakeroot). **added**
  `scripts/hpc3_vlreview_setup.sh`. Wired the render loop into `app.py::_postrender_visual_check`
  (+ new `gateway/vlreview_runner.py`); `report.py` re-render knobs; `settings.py` gained
  `vlreview_partition` (default paid `gpu` — lab account buys priority over slow free-gpu).
  Container built + weights staged on HPC3; feature opt-in via `BIOAGENT_VLREVIEW_ENABLED=1`.
  See `handoff/yijun/HANDOFF.md` (2026-07-02).

- 2026-07-02 · `claude` · **added** `deploy/vlreview/` (`vlreview.def`, `run_review.py`,
  `README.md`), `src/bioagent/gateway/vlreview_job.py`, and
  `src/bioagent/tools/visual_review.py` — a render-level VL review that closes Qwen3.6's
  blindness to layout defects (text overlap, clipped cells, caption-on-figure). Route C:
  short-lived cheap-GPU (A30/RTX6000, NOT A100) Singularity batch job, mirrors `deploy/scgpt/`.
  `visual_review.py` is the render→review→**re-render with escalated format** loop; residual
  defects go to the technical-report Diagnostics only. Wired into the finalization pipeline as
  `app.py::_postrender_visual_check` (sibling of `_postrender_text_check`, one-line call) via new
  `gateway/vlreview_runner.py` (stages pdf→dfs3b, runs the job, reads review.json back — mirrors
  `scgpt_runner.py`); residual diag threaded into `_build_technical_report(render_diag=...)`.
  HPC3 build/weights one-shot: `scripts/hpc3_vlreview_setup.sh` (mirrors `hpc3_vllm_setup.sh`;
  custom .def → needs --fakeroot/--remote, unlike the vLLM `singularity pull`). Also **edited**
  `tools/report.py` (new `build_pdf_report(format_overrides=...)` + `DEFAULT_FORMAT`/builder fns —
  the knobs the loop escalates) and `gateway/settings.py` (new `vlreview_*` fields + env parsing,
  opt-in via `BIOAGENT_VLREVIEW_ENABLED`). Feature is behaviorally inert until the .sif is built
  on HPC3 AND the flag is flipped. Eyeserver deploy is the operator's (sync_deploy.sh).

- 2026-07-01 · `claude` · **added** `src/bioagent/gateway/email_send.py` (pluggable SMTP sender
  + dev/log fallback), `tests/test_registration.py`, and `docs/self_registration.md` — for the
  new self-registration channel (UCI email + emailed 6-digit code) and admin
  email/search/delete. New ORM table `pending_registrations` (in `models.py`; auto-created by
  `init_db`/`create_all`). Registration + admin routes added to `auth_routes.py`; login +
  admin UI in `frontend/console/{index.html,app.js,styles.css}`. SMTP via env
  (`BIOAGENT_SMTP_*`); unset → dev mode (code logged, returned to the browser for local test).

- 2026-07-01 · `claude` · **added** `docs/frontend_ux_fixes.md` — task list + tracker for the
  `fix/frontend-ux-batch` branch (7 researcher-facing console fixes: cross-chat result leak,
  streaming refresh, run_code collapse + step summary, log→bundle, Material Symbols icons,
  Runs single-zip, folder upload). No new source dirs; edits touch `frontend/console/*`,
  `src/bioagent/gateway/app.py`, `agents/{research_harness,research_lab,sandbox}.py`. (A
  transient `.claude/launch.json` static-preview config was created for a visual smoke test
  and deleted — never committed.)

- 2026-07-01 · `claude` · **restored** `skills/*/scripts/*.py` folders + added progressive
  disclosure — **reverses the inline change below**. Scripts are files again (lint/test-able),
  but the Scientist's per-step brief now lists only a MANIFEST (script name + one-line summary
  = first module-docstring line); the full body is fetched on demand via a new
  `read_skill_reference(name)` tool (wired in `ResearchLab._make_skill_reference_tool`, closes
  over the selected skill). Why: folders-alone didn't save context (loader was eager); the real
  lever is progressive disclosure, which also makes "template needs a local tweak" trivial
  (fetch→adapt→run). `agents/presets.py`: `_extract_scripts` → `_load_scripts` + `_script_summary`;
  `SkillScript` gains `summary`. 43 tests pass.

- 2026-07-01 · `claude` · **[SUPERSEDED by the entry above]** removed `skills/*/scripts/` folders
  (all 4 skills); reference code inlined in each `SKILL.md` under a `## Reference code` section.
  Committed as `b0f11ec`, then reverted the same day — inline saved nothing over folders (eager
  loader) and gave up lint/test-able script files.

- 2026-07-01 · `claude` · **added** `deploy/public-domain-tls.md` — end-to-end runbook for the
  AiScientist/MMFatlas TLS certs (CSR → Pablo issuance → verification → k8s install → renewal +
  private-key custody). The public-domain-config doc Jin requested; certs verified good, k8s
  install (steps 5–6) still pending cluster access. Cert/key files live outside git in
  `~/aiscientist-certs/` (never committed).

### 2026-07-01 — `claude` — data-boundary guard tests (structural rewrite, option ②)

New:
- `tests/test_data_boundary_guard.py` — unit + harness tests for `DataBoundaryGuard` after the
  rewrite: raw data judged by a numeric-grid structure (not comma-counting), raw-data sniffing
  source-scoped to the untrusted user span, secrets always blocked. Landed via branch
  `feat/critic-evidence-pointers` (merged to main).

### 2026-06-30 — `claude` — analysis.sif build kit for HPC CodeAct

New:
- `deploy/analysis/analysis.def` — CPU Singularity recipe (scanpy/pandas/gseapy/leidenalg +
  scikit-misc/igraph/psutil, mirrors pyproject `[analysis]`); the image `SlurmCodeExecutor` runs
  `run_code` snippets inside on HPC3. `deploy/analysis/build_and_stage.sh` (build → stage to dfs3b →
  smoke) + `deploy/analysis/README.md` + `.gitignore` (`*.sif`). Mirrors the `deploy/scgpt/` kit.

### 2026-06-30 — `claude` — report-quality fixes: literature query, run_code context, HPC exec, degradation channel

New:
- `src/bioagent/gateway/slurm_sandbox.py` — `SlurmCodeExecutor`: runs CodeAct `run_code` snippets
  as Singularity-contained CPU **Slurm batch jobs on HPC3** (real `#SBATCH --mem` cap → fixes the
  OOM/-9 kills), reusing `gateway/slurm_job.py`. Opt-in via `BIOAGENT_RUN_CODE_ON_HPC`; falls back
  to the local `CodeSandbox`. Lives in the **gateway** layer (it depends on `RemoteExecutor` /
  `slurm_job`) so `agents/` stays decoupled from `gateway/` per the layering convention.
- `tests/test_slurm_sandbox.py` — offline tests for the above (scripted fake `RemoteExecutor`).

Content edits (no structural change; listed for orientation):
- `tools/literature_references.py` — reference query now built from real science (agenda subject +
  in-loop `literature_search` queries), not the bare UI prompt; harvests on-topic in-loop citations.
- `agents/sandbox.py` + `agents/research_lab.py` — inject live execution context (obs schema, real
  paths, CWD/OOM caveats) into the `run_code` tool description.
- `gateway/app.py` — `_summarize_pipeline_degradations` / `_step_failures`: step degradations
  (max_steps, tool/OOM failures) now flow ONLY into the technical report's Diagnostics; wire the
  HPC executor selection. `gateway/settings.py` — CPU-analysis Slurm settings.
- `skills/README.md` (+ `differential_expression/SKILL.md`) — documented the HPC3 sbatch example +
  run_code memory-safety.

### 2026-06-30 — `claude` — data-aware PI planner + literature-query fix (branch `fix/report-output-and-file-browser`)

New:
- `tests/test_dataset_preflight_obs.py` — covers the new `_obs_categoricals` preflight
  extraction + that `inspect_h5ad` attaches `obs_categoricals`.

Content edits (no structural change; listed for orientation — these are ENGINE/`.py` fixes,
NOT new `skills/*/SKILL.md`, because they add a capability + change what data flows into the
PI prompt, which a steering-prompt skill cannot do):
- `src/bioagent/tools/datasets.py` — preflight now extracts categorical obs values
  (`obs_categoricals`: e.g. `sampleid=[DDX41,WT]`, `majorclass=[...]`); high-cardinality
  columns keep only their count.
- `src/bioagent/agents/research_lab.py` — `_dataset_context()` feeds the dataset profile into
  the PI's planning prompt; `_PI_SYSTEM` gains design-aware rules (compare condition/group
  columns; reuse existing label columns instead of de-novo annotation). Makes the existing
  `skills/differential_expression/` skill actually reachable on a KO-vs-WT dataset.
- `src/bioagent/tools/literature_references.py` + `gateway/app.py` — `derive_reference_query()`
  searches the run's real scientific subject (manuscript title/agenda), not a meta-instruction
  question (the cause of the irrelevant pedagogy citations).
- `tests/test_literature_references.py`, `tests/test_research_lab.py` — added coverage.
- `skills/README.md` — added a "Which layer does my change belong in?" decision rule
  (skill vs tool vs agent vs engine) so future features default to a SKILL.md, not engine code.
- `skills/differential_expression/scripts/condition_by_celltype.py` (new) — reference template
  for stratified condition-vs-control DE (per cell type) + volcano + shared up/down genes,
  matching the gold-standard DDX41 report shape.
- `skills/differential_expression/SKILL.md` (content) — upgraded: defaults to per-cell-type
  condition-vs-control and INFERS the comparison from the dataset's obs profile (condition column
  + existing labels), so a non-biologist can run it with a trivial/empty question.

### 2026-06-29 — `claude+user` — skills/ migration-development: reference code + 2 new skills (branch `feat/axis-b-pi-skill-selection`)

New:
- `skills/differential_expression/` (SKILL.md + `scripts/pairwise_de.py`) — A-vs-B group DE,
  the gap `run_de`'s per-cluster mode doesn't cover.
- `skills/gene_signature_scoring/` (SKILL.md + `scripts/score_signature.py`) — per-cell
  `sc.tl.score_genes` signature scoring, a CodeAct gap.
- `skills/celltype_annotation/scripts/annotate_clusters_by_markers.py`,
  `skills/scgpt_annotation/scripts/crossvalidate_scgpt_vs_leiden.py` — reference templates
  for the two migrated skills' tool-gaps (marker→label assignment; scGPT↔Leiden confusion).

Why: the "migration-development" phase of the operon-style skill library — add vetted
reference code (CodeAct templates), decouple skills into self-contained packages, expand
the library from 2 → 4 protocols. Scripts are TEMPLATES the Scientist adapts via run_code,
not auto-run code; they call the registered tools' checkpoints, never reimplement a tool.

Changed (content, for orientation):
- `skills/*/SKILL.md` (celltype, scgpt) — decoupled: `tools:` frontmatter added, bodies made
  mode-agnostic + naming the tools + referencing the bundled script. `skills/README.md` —
  documents the `tools:` field + `scripts/` convention (BIOAGENT_WORK/ARTIFACTS env).
- `src/bioagent/agents/presets.py` — `SkillScript` dataclass; `ResearchPreset` gains
  `tools`/`scripts`; loader parses `tools:` + loads `scripts/*.py`; `list_presets()` adds
  `tools`/`scripts` (additive). `agents/research_lab.py` — the auto-selected skill's
  reference scripts are surfaced in the Scientist's per-step brief.

### 2026-06-29 (L2 hint) — `claude` — branch `fix/report-output-and-file-browser`: "reattach" hint after a gateway restart (frontend only)

KEY FINDING (so nobody rebuilds it): the expensive part of L2 — reattaching to a still-running
GPU job after a gateway restart — ALREADY works. `gpu.find_running_job`/`ensure_serve_job` reuse
the user's running `squeue --me` job and read its port from HPC3's `$HOME/.bioagent/vllm.port`
(state lives on HPC3, survives a gateway restart). So a fresh login reattaches (no re-queue, no
model reload); only SSH+Duo re-auth is unavoidable. No backend persistence/auto-revive needed.

Change (frontend only, no .py touched): when `restoreConnection()` finds the stored connection_id
dead (gateway restarted), show a dismissible login-screen banner — "your GPU job is likely still
running; log in to reattach automatically" — and prefill username/host from a new `LASTCONN_KEY`
localStorage record written while a session is ready. Edits: `frontend/console/index.html`
(banner markup), `app.js` (`LASTCONN_KEY`, `showReattachHint`, restore-path + dismiss wiring),
`styles.css` (`.reattach-hint`). Pure UI; reattach itself was already automatic.

### 2026-06-29 — `claude` — branch `fix/report-output-and-file-browser`: L1 session reconnect (refresh/back no longer loses the live run)

New:
- `tests/test_connection_replay.py` — offline tests for `Connection._track_stream` /
  `stream_replay_payloads` (in-flight assistant turn rebuilt for a reconnecting client) and
  `chat_running` in `summary()`. No cluster/SSH/network.

Why: refreshing or accidentally navigating back dropped the WS and the client FORGOT its
`connection_id` (kept only in JS memory, never persisted), so the user lost all view of an
in-flight run and thought they had to rerun — even though the server-side Connection + asyncio
run task were still alive. Fix (L1 of a 3-layer plan; L2 cross-restart reattach + L3
checkpoint/resume deferred): persist `connection_id` to localStorage, re-validate + re-subscribe
on boot, and replay the live centre bubble. Content edits: `src/bioagent/gateway/app.py`
(`Connection.stream` buffer + `_track_stream`/`stream_replay_payloads`, `chat_running` in summary,
WS endpoint replays the in-flight turn while `chat_running`), `frontend/console/app.js`
(`CONN_KEY` persist/clear, `restoreConnection()` on bootstrap, `applyStatus` honours
`chat_running`). Code + offline smoke test only — frontend not run locally (remote-tunnel debug).
NOTE: this branch now bundles THREE logical changes (file-browser/report-output, then this L1
reconnect) on top of `feat/streaming-lab-progress`; split when turning into PRs.

### 2026-06-28 (report+files) — `claude` — branch `fix/report-output-and-file-browser` (stacked on streaming): report titles, no-data warning, matplotlib fix, folder file browser

New:
- `tests/test_report_output.py` — offline tests for `_promote_doc_title` (content-derived
  doc titles) and `CodeSandbox._env` (writable `MPLCONFIGDIR`). No cluster/SSH/network.

Why (from a real single-cell run that produced a figure-less report): (1) report PDF/DOCX were
titled by the hardcoded "BioAgent Research Report" — now `_promote_doc_title` promotes the
report's own first H1 (the main finding) to the pandoc title and strips it from the body
(`src/bioagent/gateway/app.py`). (2) The run was launched with NO dataset, silently — every
scanpy tool returned "no dataset loaded" so the report had no figures, and the user couldn't
tell why; `_run_lab` now emits a LOUD warning + key-progress line when no dataset is attached.
(3) The agent's manual run_code plotting died on "Permission denied creating matplotlib cache
directories" in the Singularity container — `CodeSandbox._env` now pins `MPLCONFIGDIR` (+
`XDG_CACHE_HOME`) to a run-owned writable dir (`src/bioagent/agents/sandbox.py`). (4) The
Downloads panel dumped every file as a flat link ("散落"); `frontend/console/app.js`
(`renderDownloads`/`loadResults`) + `styles.css` now show ONE zip + thumbnails-on-top +
a folder-grouped directory list below; the in-chat artifacts block is compact (zip + pointer
to the panel). Code + offline smoke test only — frontend not run locally (remote-tunnel debug).

### 2026-06-28 (streaming) — `claude` — branch `feat/streaming-lab-progress`: live lab progress in the centre panel

New:
- `tests/test_lab_progress_stream.py` — offline smoke test for `_lab_event_to_chat`, the pure
  event→chat-payload mapper behind the live progress feed (no cluster/SSH/network).

Why: a lab run only filled the centre chat bubble at the very end (one `chat_token` dump of the
finished report) while the right-hand log got all the live `emit()` events — so the "left" sat at
"…" until done. Now `_run_lab`/`on_event` also stream the run into the bubble Claude-style: verbose
turns (tool calls, critic) → the collapsible `chat_thinking` activity log; key milestones (plan,
each step, acceptances, report phases, done) → a new `lab_progress` WS message rendered as an
always-visible key-progress feed. Content edits to `src/bioagent/gateway/app.py` (new
`_lab_event_to_chat`, `say_key`, report-phase progress lines), `frontend/console/app.js`
(`.lab-progress` element + `appendLabProgress` + `lab_progress` case), `frontend/console/styles.css`
(`.lab-progress` styles). Code + offline smoke test only — frontend not run locally (remote-tunnel debug).

### 2026-06-28 (even later) — `claude` — disk hygiene: manual dataset delete ONLY (auto-retention dropped)

New:
- `tests/test_dataset_delete.py` — offline test for the `delete_dataset_record` ownership
  helper (owner-only, manual deletion).

Why: uploaded datasets had no delete path. Added `POST /api/datasets/delete` (+ Datasets >
Delete button). An automatic run-retention sweep was first added then **removed at Yijun's
call** — auto-deleting "expired" research data risks losing data that's actually important;
that risk outweighs storage cost. Deletion is now always user-initiated. Content edits to
`gateway/app.py`, `gateway/auth_routes.py`, `frontend/console/app.js`, `styles.css`.

### 2026-06-28 (later) — `claude` — literature cost/caching handoff for the literature line

New:
- `handoff/ziyao/COST_AND_CACHING.md` — standalone, forwardable cost report for Ziyao Ma:
  Europe PMC (free, no bill) vs Crow (Edison credit-metered, exact price UNCONFIRMED), the four
  cost levers (Crow-not-Falcon, Europe-PMC-first, local cache-dedup, per-report cap), and the
  literature-line TODOs. Written at Yijun's request to report to the literature line.

Changed (content-only): `docs/literature_embedding_plan.md` (+§5 Cost), `handoff/ziyao/HANDOFF.md`
+ `.zh-CN.md` (cost & caching subsection).

### 2026-06-28 — `claude` — literature plan doc + concrete remote provider pick

New:
- `docs/literature_embedding_plan.md` — the FINALIZED literature+embedding plan (supersedes the
  old "选型清单" that lived only in WeChat). Decision: Tier 1 = FutureHouse/Edison Scientific
  platform (agent Crow, PaperQA2-based) → Tier 2 = Europe PMC keyword. No local embedding. Records
  the Mode-B (front-load retrieval into writing) next step. Mirrored to the user's external copy.

Changed (content-only): `src/bioagent/tools/literature_references.py` Tier-1 docstrings now name
the concrete provider (FutureHouse/Edison Crow) + integration path; the env-gated generic REST tier
is unchanged in behaviour.

### 2026-06-27 — `claude` — literature REFERENCE module (fills the manuscript's `## References` slot)

New:
- `src/bioagent/tools/literature_references.py` — the missing "literature module" the report
  writer's reserved `## References` placeholder always promised. Tiered retrieval: remote
  one-stop RAG service (Tier 1, env-gated `BIOAGENT_LITERATURE_REMOTE_URL`) → Europe PMC
  keyword fallback (Tier 2, reuses `literature_search.search_europepmc`). Fills the slot with
  REAL citations (never fabricates), returns a `degradation_note` for the technical report.
- `tests/test_literature_references.py` — 11 tests (tier selection, privacy, insertion,
  empty/honest-none, degradation note).

Changed (content-only, no structure change): `src/bioagent/gateway/app.py` wires the module
into the report pipeline (fills references before self-review; threads the fallback note into
`_build_technical_report`); the self-review + writer prompts updated to preserve the now-filled
References section.

### 2026-06-27 — `claude+user` — `skills/` research-path library (operon-style)

New:
- `skills/` (repo root) — operon-style (`swaruplab/operon`) skill library. One folder
  per research path: `skills/<name>/SKILL.md` = frontmatter (`name` + `description`) +
  markdown body that is the PI's default planning guidance.
- `skills/celltype_annotation/SKILL.md`, `skills/scgpt_annotation/SKILL.md` — the two
  former hardcoded presets, migrated verbatim (body text unchanged).
- `skills/README.md` — format + the tool-vs-skill boundary.

Why: decouple the *workflow layer* from Python so adding a research path = dropping a
folder (no code change), seeding the operon protocol-library pattern on our
PI → Scientist → Critic loop.

Changed (content, noted for orientation):
- `src/bioagent/agents/presets.py` — was hardcoded `ResearchPreset` constants; now a thin
  loader that reads every `skills/*/SKILL.md` into `PRESETS` (`name`→key, `description`→
  label, body→prompt). Public API unchanged (`PRESETS`/`get_preset`/`list_presets`);
  `$BIOAGENT_SKILLS_DIR` overrides the location. Deferred to migration phase: `scripts/`
  reference code, `references/`, description-based selection at scale.

### 2026-06-26 — `claude` — PaperQA HPC3 real-machine smoke

New:
- `scripts/paperqa_hpc3_smoke.py` — TEMPORARY diagnostic. Drives the `deep_literature`
  (PaperQA2) tool on HPC3 against the live local Qwen by injecting a PI agenda (a list of
  literature questions) straight into `run_paperqa`, bypassing the PI/Scientist/Critic
  planning loop. Pre-flights paper-qa import, /v1/models model-name match, and the
  privacy boundary (LLM api_base=127.0.0.1, embedding=st-), then reports per-question
  status/answer/citations. Throwaway — delete once ziyao's HANDOFF Open Items #1–#5 are
  verified on the box.

### 2026-06-26 — `claude` — BioAdmin injection script + deploy version fingerprint

New:
- `scripts/inject_admin.py` — standalone script to re-seed a BioAdmin account directly
  in the gateway SQLite/Postgres DB when `bioagent-admin` CLI is unavailable or env
  isn't loading.  Run as bioagent on the server; prompts for password (or reads
  BIOAGENT_ADMIN_PASSWORD env var for non-interactive use).

Modified:
- `scripts/deploy_interactive.sh` — writes `${APP_DIR}/.deployed_sha` (sha + branch +
  UTC timestamp) after every deploy so local vs server version is diffable via SSH.

Branch created:
- `feat/paperqa-guard-critic` — branch off main for Ziyao's pending paperqa changes 5+6
  (numeric-only raw-data guard + auto-accept on objective success). See `handoff/ziyao/`.

### 2026-06-20 — `claude` — handoffs reorganized into per-line folders

Moved (git mv, history preserved):
- `HANDOFF.md`, `HANDOFF.zh-CN.md` → `handoff/yijun/` (core/orchestrator line).
- `HANDOFF.deep-literature.md`, `HANDOFF.deep-literature.zh-CN.md` → `handoff/ziyao/HANDOFF.md` +
  `handoff/ziyao/HANDOFF.zh-CN.md` (literature line, from Ziyao's PaperQA2 PR #3).

New:
- `handoff/README.md` — index + convention: each line/owner updates only its own handoff.

Updated references to the moved paths: `scripts/pr_review_gate.py` (required-files check now
points at `handoff/yijun/...`), `README.md` (2 links), `src/bioagent/lab/__init__.py` (comment),
`CLAUDE.md` (new "Handoff docs" convention). Root `README.md`/`README.zh-CN.md` stay at root.

### 2026-06-20 — `claude` — deploy/sync scripts for the eyeserver

New:
- `scripts/deploy_interactive.sh` — COWORKER-friendly deploy: no key/NOPASSWD setup; prompts
  for the operator's own `<user>-admin` password (SSH once + sudo once via ControlMaster +
  `ssh -t`), rsyncs local → staging → app as `bioagent`, restarts, health-checks. Use this
  when the deployer only has password-based `-admin` sudo (the eyeserver model: real login
  accounts have NO sudo; the `<user>-admin` accounts are in the sudo group; `bioagent` is a
  nologin service account with no password).
- `scripts/sync_deploy.sh` — rsync the LOCAL working tree → eyeserver `/data/BioAgent/app`
  and restart the console. Used because the server's git remote is the PRIVATE repo with
  no creds (can't `git pull`), so rsync-from-local is the reliable sync path. Excludes
  `.env`/DB/venv/`runs/`/`.git`; `--delete` for an exact overwrite of a stale server tree;
  team setup (shared `bioagent` SSH key) documented in the header. Usage is in the file.

### 2026-06-20 — `claude` — scGPT lab-integration (engine → tool → runner → preset)

New (committed on `scGPT-workflow-and-k8n-online`):
- `src/bioagent/gateway/scgpt_runner.py` — gateway-side runner injected into the catalog
  (stage `.h5ad` via SFTP → `run_scgpt_inference` → fetch predictions → summarise labels).
- `src/bioagent/tools/scgpt_annotate.py` — the `scgpt_annotate` HarnessTool (always in the
  catalog; not-enabled without a runner).
- `tests/test_scgpt_job.py`, `tests/test_scgpt_annotate.py` — offline tests (fake executor).
- `docs/scgpt_workflow_integration.md` — design + gap analysis + deployment plan.

Modified (content, for orientation):
- `src/bioagent/gateway/{scgpt_job.py, slurm_job.py, settings.py, executor.py, ssh_gateway.py,
  mock_host.py, app.py}` — `gres=` GPU batch jobs, `put_file`/`get_file` (SFTP staging),
  scgpt image/model settings, runner wiring.
- `src/bioagent/agents/{registry.py, presets.py}` — `scgpt_runner=` param; `SCGPT_ANNOTATION` preset.
- `src/bioagent/tools/research_bundle.py` — transcript surfaces per-tool ✓/✗ + a debug summary.
- `deploy/scgpt/scgpt.def` — `chmod -R a+rX /opt/scgpt` (vendored sources arrived mode 600/700).
- `HANDOFF.md` — "2026-06-20" section (scGPT deployed + lab-integrated).

### 2026-06-20 — `claude` — System workflow-graph viewer + DRAFT multi-agent lab

New (uncommitted at time of writing):
- `src/bioagent/lab/` **(new package)** — DRAFT multi-agent "Virtual Lab" kernel for the
  week-of-2026-06-22 discussion. `archive.py` (durable Lab Archive), `kernel.py` (fixed
  dispatcher + Tool/Agent/Playbook registries), `__init__.py`. **NOT wired into the gateway.**
- `tests/test_lab_kernel_draft.py` **(new)** — offline tests for `src/bioagent/lab` (5 tests, no LLM/GPU).
- `frontend/console/cytoscape.min.js` **(new, vendored)** — Cytoscape.js for the System-page
  workflow graph. **Should be committed** (runtime dep served from `/static`; no-build frontend).
- `bioagent.db` **(new, local SQLite — DO NOT COMMIT)** — created as a side effect of running
  the gateway locally to view the System graph; now gitignored via `*.db`.

Modified (content, for orientation):
- `src/bioagent/gateway/system_info.py` — added `workflow_graph()` (live node/edge spec) + into `/api/system`.
- `frontend/console/{index.html, app.js, styles.css}` — read-only workflow-graph panel on the System page.
- `tests/test_system_info.py` — assertions for the new graph.
- `HANDOFF.md`, `HANDOFF.zh-CN.md` — "2026-06-18 (later)" section (multi-agent reinstated + Lab Archive draft).
- `.gitignore` — ignore `*.db` / `*.sqlite*`; `.claude/filemanager.md` (this file) added.

### 2026-06-17..18 — `user` (some `claude+user`) — already committed milestones
- `deploy/scgpt/` — scGPT Singularity build kit (vendor `scGPT_refactor`, route B):
  `run_infer.py`, `scgpt.def`, `build_and_stage.sh`, `README.md`. (Drafted by `claude`,
  reworked + committed by `user`.)
- `src/bioagent/gateway/scgpt_job.py` — Route C GPU batch-inference engine for scGPT.
- `deploy/` (k8s kit) — public Kubernetes deployment (see HANDOFF 2026-06-18).
