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

### 2026-09-30 — `claude` — public mirror: publish script; `.publicexclude` emptied
- **Added:** `scripts/publish_public_mirror.py` (builds the RCHENLAB/AiScientist snapshot, checks
  every file for credentials, syncs into a clone; never commits or pushes) and
  `tests/test_publish_public_mirror.py`.
- **Changed:** `.publicexclude` keeps its header but lists no paths. The decks, `docs/decks/build_deck.js`,
  `reports/aiscientist-manual/`, `RETIGENE_PAPER_RECOVERY_HANDOFF.md` and the proposal PDF are now
  published.
- **Why:** Yijun's policy, 2026-09-30: the public mirror carries everything except credentials.

### 2026-09-30 — `claude` — one folder per tool; docs/architecture; split tooling
- **Moved / split (by `scripts/refactor/split_tools.py`, re-runnable):** `tools/dataset_inspect.py` →
  `tools/inspect_dataset/tool.py`; `literature_search.py` → `literature_search/tool.py`; `paperqa_search.py`
  → `deep_literature/tool.py`; `variant_annotation.py`, `vcf_offline.py`, `ird_annotate.py`,
  `ird_prioritize.py` → `annotate_variants/` (`tool.py`, `offline.py`, ...); `phenotype_evidence.py` →
  `diagnose_disease/evidence.py`; `schematic.py` → `make_schematic/tool.py`; `scgpt_annotate.py` →
  `scgpt_annotate/tool.py`; `hpo_terms/` → `map_phenotype_to_hpo/` (`mapper.py` → `tool.py`).
  `scrna_pack.py` + `scrna_advanced.py` → eleven `run_*/tool.py` folders + `tools/_lib/scrna.py`;
  `phenotype_dx.py` → `run_lirical/tool.py` + `diagnose_disease/tool.py`.
- **Added:** `tools/<name>/TOOL.md` (20; manifest + documentation), `tools/README.md` (generated
  index), `tools/catalog.py` (discovery), `tools/_lib/`, `scripts/tool_docs.py` (doc generator),
  `scripts/refactor/split_tools.py` and `scripts/refactor/extract_repos.py` (split tooling),
  `docs/architecture/` (README + REPO_SPLIT, en + zh-CN), tests `test_tool_catalog.py`,
  `test_tool_manifests.py`, `test_registry_manifests.py`, `test_tool_contract.py`,
  `test_tools_boundary.py`, `test_skills_library.py`, `test_pysrc_sync.py`, `test_tools_data_paths.py`.
- **Unchanged on purpose:** the job entry points `tools/{scrna,variant,phenotype,paperqa}_cli.py`
  (baked into image runscripts), `tools/genesets/` (the deploy fills it), `tools/gene_panels/`.
- **Why:** prepare the split into AiScientist / AiScientist-tools / AiScientist-skills with low
  coupling and documentation per tool (`docs/architecture/REPO_SPLIT.md`).

### 2026-09-30 — `claude` — platform code moved out of `tools/` (`reporting/`, `hpc/shell.py`)
- **Moved (git mv, history kept):** `tools/report.py`, `tools/research_bundle.py`,
  `tools/visual_review.py`, `tools/vlreview_run.py`, `tools/literature_references.py` →
  `src/bioagent/reporting/` (new package); `tools/hpc_shell.py` → `src/bioagent/hpc/shell.py`.
  The vision-review job now runs `python -m bioagent.reporting.vlreview_run`;
  `deploy/vlreview/run_review.py` stays byte-identical to it (its test checks the new path).
- **Kept in `tools/`:** `datasets.py` and `execution.py` (the dataset profile and the smoke QC/DE).
  They looked like platform code, but the profile runs inside analysis.sif as the analysis line's
  `preflight` step, so they are domain code; the platform reaches them through `tools/api.py`.
- **`tools/__init__.py`** no longer re-exports anything (it is imported at gateway start-up and
  inside the job images).
- **Why:** `tools/` is becoming the AiScientist-tools package and must hold only what the model
  calls plus its runtime. The model never calls the report renderer, the bundle writer or the HPC3
  shell session (its eight shell tools are platform tools, like `run_code`).

### 2026-09-30 — `claude` — the tools' contract (`tools/sdk.py`), public surface (`tools/api.py`), boundary test
- **Added:** `src/bioagent/tools/sdk.py`. `HarnessTool` moved here unchanged (re-exported by
  `agents/research_harness.py`), plus the `ToolContext` protocol and `session_chat_fn` /
  `register_llm_backend`, which replace the tools' direct imports of `gateway.vllm_client`
  (`gateway/vllm_client.py` registers itself at import). The per-run `SITECUSTOMIZE` text moved from
  `gateway/package_cache.py` to `tools/run_deps.py` (package_cache imports it back).
- **Added:** `src/bioagent/tools/api.py`, the only door from the platform into the tools besides
  `sdk`: a lazy name table (private helpers get public names here) and the HPC3 job entry-point
  module paths. Every gateway/agents import of a tool module now goes through it.
- **Added:** `tests/test_repo_boundaries.py`: tools import nothing from the platform; the platform
  imports tools only through `sdk`/`api`; every `api` name resolves; `sdk` is stdlib-only.
- **Why:** first step of the three-repo split (AiScientist / AiScientist-tools / AiScientist-skills,
  branch `claude/aiscientist-architecture-refactor-b0fcaa`). With the contract in place, the tools can
  be reorganised one folder per tool without touching the platform.

### 2026-09-30 — `claude` — add `src/bioagent/tools/run_deps.py` + `tests/test_run_deps.py`
- **Added:** `src/bioagent/tools/run_deps.py`. When a curated analysis tool reports a DECLARED
  dependency missing (`ALLOWED`, pinned in code; first entry scikit-image for doublet detection),
  the Slurm analysis executor installs it into the run's own HPC3 workspace (`<run>/_deps`) and
  retries the tool. It runs in-container through `scrna_cli` (`_install_dependency`, not a model
  tool) and is deleted when the run is published.
- **Added:** `tests/test_run_deps.py` (7 tests): refusal of undeclared names, pruning of what the
  image provides, appended-not-prepended import, failure cleanup, reuse. The executor cases went
  into `test_slurm_analysis.py`.
- **Why a new module:** it runs inside the container, like `scrna_cli`, so it lives with the tools
  rather than in the gateway. It is named after its scope (dependencies for one run) to keep it
  apart from the lab-shared `gateway/package_cache.py`.

### 2026-09-29 — `claude` — add `tests/test_tool_input.py`
- **Added:** `tests/test_tool_input.py` (12 tests). It pins the analysis tools' new `input` parameter,
  which lets a tool read a file a run_code step wrote instead of only the previous tool's fixed-name
  checkpoint. It covers where `input` may point (only inside the run's work/ and artifacts/), that
  the default chain is unchanged, the "QC done in run_code" dead end, and GSEA on the model's own
  `.rnk` files. The advanced tools' `input` cases went into `test_scrna_advanced.py`; the
  mitochondrial-rule cases (`mito_prefix`, `gene_symbols_key`) went into `test_tool_self_diagnosis.py`.
- **Why a new file:** the parameter spans both tool modules (`scrna_pack`, `scrna_advanced`) and the
  harness's provenance record. It is named after what it opens up, so it is findable next to
  `test_tool_result_truncation.py`.

### 2026-09-28 — `claude` — add `tests/test_tool_result_truncation.py`
- **Added:** `tests/test_tool_result_truncation.py` (8 tests). It pins the rule that the models'
  shortened views of a tool result drop data, never the keys that say how the result may be
  reported. The views are the Critic's `result_digest` (30 keys per dict) and the Scientist's
  4000-character feed (`ResearchHarness._feed_result`). The fixture is run c135ae589d96's 38-key
  run_de result, key for key.
- **Why a new file:** the rule spans the harness (feed, digest) and the lab (`_critic` payload). It
  is named after the failure, so the next "the model never saw X" search finds it.

### 2026-09-28 — `claude` — claim audit + per-role effort / no wall-clock limit
- **Added:** `src/bioagent/agents/claim_audit.py`. Before the report is written, the candidate headline
  findings are checked one short question at a time (global shift under a depth gap, gene/cell-type
  plausibility, pseudoreplicated p/FDR, causal wording). The verdicts bind both writers and are
  shown in the technical report.
- **Added:** `tests/test_claim_audit.py`, `tests/test_lab_call_no_wallclock.py`.

### 2026-09-28 — `claude` — a step's answer is checked against its own tool results
- **Added:** `src/bioagent/agents/step_numbers.py`. It is the deterministic check behind the Critic's
  second floor. It reads the per-(group, arm) cell counts a step's final answer states, from
  Markdown tables or lines like "Endothelial (WT: 7, DDX41: 27)", and compares them with what the
  same step's tools counted (`run_de` `skipped_groups` / `cells_by_group_and_arm`,
  `run_composition` `cells_by_group_and_arm`). It also treats the dataset profile's
  `design_by_arm.cells_by_label_and_arm` as a ceiling. It is its own module because the claim
  extraction is ~450 lines with no other home, and `research_lab.py` only calls three functions.
- **Added:** `tests/test_step_numbers.py`. It replays run 8847d521ba32, using that run's verbatim
  answer excerpt and real `run_de` result, with the profile computed from its `Ddx41_DEG.h5ad`. It
  also covers the precision cases that must NOT be flagged.
- **Why:** 8847's DE answer swapped the two arms of every skipped cell type and invented a split
  for the tested ones, while run_de held the right numbers. The Critic accepted it at 0.95 and the
  report copied it. The report writer had closed-set grounding; the step level had none.

### 2026-09-25 — `claude` — Qwen3.6 vs Qwen3.8 A/B results
- **Added:** `experiments/plan_vs_exec_ab/results_qwen38/` holds SUMMARY.md plus the raw A/C/judge
  jsonl for the model swap. Both models were served from our own RTX6000s. Stages A and C only;
  B (execution) was not run.

### 2026-09-25 — `claude` — the cluster model list (several weight sets on HPC3)
- **Added:** `src/bioagent/gateway/cluster_models.py`. This is the admin-managed list of weight sets
  the cluster GPU can serve, each with its own vLLM image, quantization and args. It is stored at
  `<BIOAGENT_STATE_DIR>/cluster_models.json` (server state, NOT in git) and seeded from the env plus
  `KNOWN` recipes. It powers the "Cluster GPU · <model>" options and the 🖥 dialog.
- **Added:** `tests/test_cluster_models.py`.

### 2026-09-14 — `claude` — the environment manifest: what we have and where it is
- **Added:** `src/bioagent/gateway/environment.py` — the asset/tool manifest. Where the container
  images, model weights (scGPT, VL review), `.gmt` gene-set libraries, reference data and package
  cache live, whether THIS session may read each one, and the `file:line` of every tool's
  implementation. Derived from live code + settings, so it cannot drift. Sibling to
  `system_info.py`, which answers "which agents/tools exist" for the console; this answers "where
  are the files, and may I read them" — the question that had no answer anywhere.
- **Added:** `scripts/write_environment_doc.py` — regenerates `docs/ENVIRONMENT.md` from that same
  manifest, so the human document and the agent's view cannot disagree.
- **Added:** `docs/ENVIRONMENT.md` — generated; do not hand-edit.
- **Added:** `tests/test_environment_manifest.py` — 12 tests. The load-bearing one asserts every
  tool resolves to a real `file:line`: the first version read `HarnessTool.runner` (the field is
  `executor`), got `None` for all 21, and rendered a tools table with no locations.
- **Why:** run `3c5fbc8608a7` lost two capabilities to facts nobody had written down where the
  agent could read them — the scGPT step could not reach the model directory it was told to
  verify, and the pathway step went to the network for gene sets that were already on disk in the
  directory the enrichment tools read from. Both steps behaved correctly; both produced nothing.
  Reachable by the agent as the `describe_environment` tool (progressive disclosure — fetched when
  a step needs it, never prepended to every turn).

### 2026-09-14 — `claude` — add `tests/test_literature_step_routing.py`
- **Added:** `tests/test_literature_step_routing.py` — pins the rule that a plan step's ROLE comes
  from the tool it declares, not from its prose. Covers the production failure where
  "Run `scgpt_annotate` … reference-transferred labels" was routed to the literature path (four
  `literature_search` calls, `scgpt_annotate` never invoked, Critic 0.1, then force-advanced
  without retry because literature steps do not retry), plus the same trap for `annotate_variants`
  / `map_phenotype_to_hpo` / `diagnose_disease`, and the reverse regression (a genuine
  `literature_search` step must still classify as literature).
- **Why a new file:** the routing rule is agent-loop behaviour shared by the linear loop, the DAG
  scheduler, the literature backfill and step scoring (13 call sites) — it is not "a research_lab
  detail", and naming it after the routing decision is what makes it findable the next time a
  planned tool mysteriously never runs.

### 2026-09-14 — `claude` — add `tests/test_llm_cost_controls.py`
- **Added:** `tests/test_llm_cost_controls.py` — covers the cost controls on the LLM paths: the
  per-role remote output ceiling (`vllm_client.lab_max_tokens`), the local `reasoning_effort`
  field, `complete_ex` returning the provider's `usage`, and the `_call_with_role` adapter that
  lets an injected `complete_fn` receive a role without breaking the `(messages) -> str` contract
  every test double and the lab kernel share.
- **Why a new file rather than extending `test_vllm_client_dialect.py`:** that file is about
  served-model *dialect* knobs (chat templates for a given model). These are *spend* controls, and
  they cut across `vllm_client`, `research_lab` and the gateway — a separate name is what makes
  them findable when a bill, not a model, is the thing being debugged.
- Related (content-only, no entry needed): `gateway/models.py` gained an `llm_calls` table,
  `gateway/auth_routes.py` gained `record_llm_call` / `run_llm_cost`, and the two preset pipelines
  `scgpt_annotation` + `celltype_annotation` gained an interpretation-evidence step.

### 2026-09-05 — `claude` — add `reports/aiscientist-manual/`
- **Added:** `reports/aiscientist-manual/` — the bilingual AiScientist technical report in
  manuscript form (Abstract / Introduction / Methods / Results / Discussion / Supplementary),
  written for Jin's review request. Contents: `manual.zh.md` + `manual.en.md` (sources),
  `build.sh` (one command → 2 PDFs + 2 DOCX), `header.tex` (LaTeX preamble: table row rules,
  CJK fonts, float placement), `addrules.py` (post-processes pandoc's `longtable` output to
  draw a rule between every body row), `ref-bordered.docx` (pandoc reference doc patched to
  give the `Table` style real borders — the stock one has none), and `shots/` (report-page
  renders from run `c135ae589d96`, run figures, and production UI screenshots).
- **Why here, not a scratch dir:** an earlier copy lived in the session scratchpad under
  `/private/tmp` and was reclaimed by the OS, losing the sources. `reports/` already holds the
  dated progress reports, so the manuscript belongs beside them.
- Build: `./reports/aiscientist-manual/build.sh` (needs pandoc, tectonic, and the CJK system
  fonts Songti SC / PingFang SC). Generated `*.pdf` / `*.docx` are committed alongside the
  sources so a reader does not need the toolchain.
- **Committed to the private repo only** — added to `.publicexclude`, so the public
  RCHENLAB/AiScientist mirror strips it. It carries unpublished DDX41 results, internal UI
  screenshots and unreleased-model deployment measurements.

### 2026-09-04 — `claude` — rescue `deploy/mmfatlas-service.md` from a dead-history branch
- **Replaced (content, 161 -> 376 lines):** `deploy/mmfatlas-service.md`, taken verbatim from
  `claude/mmfatlas-service-setup-7ca4e1` @ `481003a`. Not a merge: that branch shares **no
  merge-base** with `main` (it sits on the pre-reset history — see the branch-topology note), so
  its five 2026-08-27 commits could never have arrived by merging. They touch only this one file,
  which is why lifting the file is both sufficient and safe.
- Why it was worth rescuing: it supersedes the old note rather than extending it — the confirmed
  root cause (the app's listening port moves between 5005/5006 while the Service's `targetPort` is
  hardcoded), the live cluster edits applied on 2026-08-27 with their backup paths, a readiness
  probe, the ownership boundary (MMFAtlas is **Texera's** CELLxGENE instance, not ours), and a
  5-command diagnosis runbook. Both prior incidents (2026-07-02, 2026-07-30→31) survive in a
  "Failure history" table, and the personal admin account the old version named is gone.
- The rest of that branch is pre-reset history and stays archived under
  `archive/legacy-history/claude/mmfatlas-service-setup-7ca4e1`.

### 2026-09-04 — `claude` — track `docs/decks/` (it was untracked, and held an irreplaceable source)
- **Added (previously untracked):** `docs/decks/build_deck.js` + `docs/decks/AiScientist_Technical_Spec_EN.pptx`
  + `docs/decks/AiScientist_技术规格说明_中文.pptx` — the 2026-07-28 technical-spec deck. Found during a
  worktree audit as the only untracked path in `main`'s working tree. The `.pptx` pair can be
  regenerated; `build_deck.js` cannot — it is a 95 KB pptxgenjs source that exists nowhere else
  (the similarly named `~/Documents/BGI-Interview/build_deck.js` is a different, larger file), so
  it was one `rm -rf` from gone. All four checksums differ from `reports/AiScientist-*.pptx`, so
  this is a separate deck, not a duplicate.
- Tracking the `.pptx` follows the existing precedent in `reports/`; all three are added to
  `.publicexclude` for the same reason those are — the slides carry unreleased-feature status, and
  the build script carries the same content in source form.
- Convention reminder, per the deck rule: **edit `build_deck.js` and regenerate; never edit a `.pptx`
  by hand.**

### 2026-08-19 — `claude` — add `tests/test_plan_review_report_v25.py`
- **Added:** `tests/test_plan_review_report_v25.py` — regression tests for the plan-mode defects in
  Ziyao's `plan_mode_report_v2_5` (B-1 bare-number render / B-3 unregistered tools + illegal param
  values / B-4 silent redraft loss / B-5 absent-step reference / B-6 ambiguous reference /
  B-7 stale downstream threshold / C-3 timeout-vs-cancel attribution). Each case is pinned to the
  production string that produced it, so a later refactor cannot quietly reintroduce a UX failure
  that took two days of manual testing to characterise. No structural change beyond this one file;
  everything else in the fix batch is edits to existing modules.


### 2026-08-19 — `claude` — add `deploy/dsv4/` (README, serve sbatch, sm_120 mHC patch); `tests/test_vllm_client_dialect.py`
- **Added:** `deploy/dsv4/` — DeepSeek-V4-Flash serving kit + the sm_120 blocked-verdict README;
  `tests/test_vllm_client_dialect.py` — env-gated served-model dialect knobs in `vllm_client`
  (`BIOAGENT_VLLM_THINK_ON_KWARGS`, `BIOAGENT_SCIENTIST_MAX_TOKENS`, `BIOAGENT_SCIENTIST_CHAT_TEMPLATE_KWARGS`).
  On HPC3 (not in git): `containers/vllm-0.27.1.sif`, `containers/vllm-nightly.sif`,
  `containers/vllm-0.27.1-patches/tilelang.py`, two NVFP4 weight sets in `hf/hub`.


### 2026-08-19 — `claude` — add `experiments/plan_vs_exec_ab/` (README.md, run_ab.py, results/)
- **Added:** `experiments/plan_vs_exec_ab/run_ab.py` — "is it the plan or the execution?" A/B: the
  real `ResearchLab._pi_plan` / `_scientist`+`_critic` / `_synthesize`→gateway `_build_report`→
  `_review_report` code paths, on the real DDX41 dataset with real local tools, swapping ONLY the
  model (prod Qwen3.6-35B no-think, Qwen3.6 think, Qwen3.5-122B, DeepSeek-V4-pro, Sonnet 5, GPT-5.4
  via OpenRouter); deterministic rubrics + blind judges. `results/SUMMARY.md` + raw jsonl.
  `src/bioagent/gateway/_app_prefix_9d72d43.py` is a transient, git-ignored copy of the pre-fix
  gateway module the script materialises for the "prefix" writer variant (not committed).


### 2026-08-19 — `claude` — add `tests/test_design_by_arm.py`
- **Added:** `tests/test_design_by_arm.py` — the profile's per-arm design table (cells / labels /
  QC medians per arm, depth-imbalance flag), run_de's direction-bias self-diagnosis, and the
  report's rendered per-arm table.

### 2026-08-19 — `claude` — add `src/bioagent/tools/vlreview_run.py`, `tests/test_vlreview_run.py`
- **Added:** `src/bioagent/tools/vlreview_run.py` — byte-identical copy of `deploy/vlreview/run_review.py`
  so the render-review Slurm job runs the LIVE reviewer from the synced pysrc (no sif rebuild);
  the test pins the two identical. Reviewer gained two deterministic detectors (text_clipped by
  page geometry, unrendered_markup by page text) after Qwen2.5-VL-7B passed a page with every
  parameter line running off the right edge and literal `**`/backticks.

### 2026-08-18 — `claude` — add `scripts/e2e_prod_drive.py`
- **Added:** `scripts/e2e_prod_drive.py` — headless end-to-end drive of the DEPLOYED gateway via
  the browser's own HTTP+WS API (connect → plan card → [question / one-step change / Stop] →
  approve → run → verify). Two runs of it found nine prod defects that 1,500+ green unit tests
  could not. Run after any change to planning, routing, offload or reporting.
- Also on HPC3 (not in repo): `uploads/<ucinetid>/Ddx41_DEG.h5ad` — a durable copy of the test
  dataset (the Temp/ copies get swept and one had, mid-test).

### 2026-08-18 — `claude` — add `tests/test_de_academic_defaults.py`; pydeps dir on HPC3
- **Added:** `tests/test_de_academic_defaults.py` (min_pct / dual-gate significance / DESeq2
  pseudobulk + loud fallback), and (on HPC3, not in the repo)
  `/dfs3b/ruic20_lab/software/AiScientist/pydeps/` — pure-Python deps the analysis image lacks
  (pydeseq2 0.4.12, `pip install --no-deps --target`), wired via `BIOAGENT_HPC_PYDEPS`.
- **Why:** measured on the real DDX41 object: run_de tested 22,387 genes of which only ~33% were
  detected (3x BH inflation + ~2,700 divide-by-zero fold-changes per stratum), and pseudobulk used
  a Welch t instead of the DESeq2 the field (and our own cited Squair 2021) expects.

### 2026-08-18 — `claude` — add `handoff/yijun/plan-mode-test-prompts.md`
- **Added:** `handoff/yijun/plan-mode-test-prompts.md`
- **Why:** the hand-off test sheet for the plan-mode work (first-round plan output, revision
  requests, and questions that must NOT redraft the plan). Written to be run by Ziyao against the
  deployed build; the ⚠️ rows in section C are the classifier misfires fixed in the same change.

- 2026-08-20 · claude · `experiments/depth_matched_validation/` (新增) —— `run_depth_matched_de` 的已知答案验证:合成两个细胞类型(纯深度 vs 真实生物学),记录 2026-08-20 在 analysis.sif 上的结果;单元测试证明不了科学正确性,这个能。
- 2026-08-20 · claude · `tests/test_conversational_turn_guard.py` (新增) —— 派发前的"这条消息里有没有研究请求"筛查(模型主判、封闭词表兜底)的回归测试;真实前端里一个 "why" 启动了完整流程。
- 2026-08-17 · `claude` · (worktree `single-cell-pipeline-review-c760de`)
  **added** `handoff/yijun/ddx41-postmortem.html` — the bilingual (ZH/EN, one source, language
  switch) post-mortem of production run `Ziyaoma/f5111e1a2382`: the eight defects and their
  attribution, the pipeline that should have run against the one that did, the causal chain from
  the dataset profiler through to the missing plan review, and an assessment of whether
  Qwen3.6-35B-A3B can carry the planning role. Also published as a private Artifact. Committed.

- 2026-08-17 · `claude` · (worktree `single-cell-pipeline-review-c760de`)
  **added** `tests/test_pseudoreplication_guard.py`, `tests/test_declared_params.py`,
  `tests/test_plan_provenance.py` — the three mechanisms recovered from reviewing production run
  `Ziyaoma/f5111e1a2382`: the `run_de` condition-column guard (lost in a rewrite, and its absence
  is how a retina .h5ad with an 11-level `majorclass` got pooled across DDX41/WT); the declared
  parameter table (`scrna_pack.PARAMS`) that the tool bodies, the model-facing schema and each
  preset's `## Parameters` section now all read, so the three copies cannot drift; and the plan
  provenance layer (`SELF-SOURCED:` disclosure, non-default-parameter reporting, read-back-step
  pruning). Committed.

- 2026-08-11 · `claude` · (worktree `vcf-normalization-variants-cef98c`)
  **added** `tests/test_tool_self_diagnosis.py` — tools report defects in their OWN output
  (`warnings`), starting with `run_scanpy_qc`: zero matched mitochondrial genes means the
  `max_pct_mt` filter was a no-op, which no count in the old result could reveal. The Critic gets
  them hoisted to the top of its payload. Single-cell/DEG line only — variant line deprioritised
  per Yijun. Committed.

- 2026-08-11 · `claude` · (worktree `vcf-normalization-variants-cef98c`)
  **added** `tests/test_evidence_resolution.py` — deterministic check that artifact paths a tool
  CLAIMS actually exist on disk before the Critic grounds a verdict on them. `evidence_pointers` was
  called in four places and nothing ever verified the files; `run_de`'s hardcoded
  `rank_genes_groups_leiden_de.png` dangled in production for every non-leiden groupby. Committed.

- 2026-08-11 · `claude` · (worktree `vcf-normalization-variants-cef98c`)
  **added** `tests/test_meeting_asymmetry.py` — team meetings now deal each expert a DIFFERENT slice
  of the accepted findings, let experts call a read-only tool whitelist (`_MEETING_TOOLS`), and make
  the synthesis report AGREED **and** UNRESOLVED. Also **added** `tests/test_team_formation_context.py`
  and `tests/test_execution_stamp.py` earlier the same day. Committed.

- 2026-08-11 · `claude` · (worktree `vcf-normalization-variants-cef98c`)
  **added** `tests/test_plan_patch.py` — a plan revision now PATCHES one step (model names it, code
  applies it) instead of re-drafting the whole agenda, with a fallback to the redraft for anything
  a single-step edit cannot express. Pins that untouched steps stay identical, that the edit is
  emitted as a before/after diff, and that step-0 / garbage / no-op replies route to the redraft.
  Measured motivation in `experiments/plan_revision_ab/`. Committed.

- 2026-08-10 · `claude` · (worktree `vcf-normalization-variants-cef98c`)
  **added** `tests/test_report_coverage.py` — pins that groups the analysis REFUSED (cell types with
  too few cells in one arm) reach the report. `_collect_facts` only recursed into dicts while both
  producers emit their skips as a list (`run_de`) or a flat dict (`run_pseudobulk_de`), so on the
  real Ddx41 data 5 of 12 cell types went untested and the grounding block never said so. Committed.

- 2026-08-10 · `claude` · (worktree `vcf-normalization-variants-cef98c`)
  **added** `src/bioagent/agents/lab_graph.py` + `tests/test_lab_graph.py` — the LangGraph execution
  shell (`LabConfig.planner="langgraph"`), step 2 of the LangGraph direction. LangGraph owns
  nodes/edges/state; `_run_one_node` (Scientist→Critic) is called unchanged from inside the graph
  nodes. The load-bearing piece is `serialize_conflicting_nodes`, which turns the scheduler's
  runtime `_concurrency_safe` check into graph EDGES, because LangGraph co-runs every ready node
  and our analysis nodes share one checkpoint chain. Optional dependency: new `langgraph` extra in
  `pyproject.toml`, deliberately NOT in `gateway`, lazily imported, tests `importorskip`. Committed.

- 2026-08-10 · `claude` · (worktree `vcf-normalization-variants-cef98c`)
  **added** `tests/test_midrun_checkpoint.py` — pins mid-run durability: `ResearchLab.run(checkpoint=…)`
  persists the run state after EVERY round, and the last snapshot before a crash round-trips through
  `ResumeState.from_run_state` (the same path `/api/lab/continue` uses). Step 1 of the LangGraph
  direction, deliberately with ZERO new dependencies. Committed.

- 2026-08-10 · `claude` · (worktree `vcf-normalization-variants-cef98c`, branch
  `claude/langgraph-framework-evaluation-f50fa7`)
  **added** `tests/test_deg_contrast.py` — the DEG line's condition-vs-reference contrast and the
  `run_de` / `run_pseudobulk_de` → `run_enrichment` hand-off. Runs REAL scanpy/gseapy on a small
  synthetic AnnData (auto-skipped without the analysis extra) because every defect it covers was an
  INTERFACE defect — what one tool writes vs what the next tool looks for — which a mocked tool
  cannot show. Committed.
- 2026-08-10 · `claude` · (worktree `elastic-nightingale-acbc89`, branch `claude/api-agent-setup-488ed1`)
  **added** `src/bioagent/gateway/llm_credentials.py` — per-user LLM API-key store, mirroring
  `ssh_credentials.py` (`<STATE_DIR>/llm_creds/<owner>/index.json` + `<id>.key` at 0600). The
  credential **id is stable and the key is a rotatable field**, so every reference to an endpoint
  survives a key rotation. At-rest encryption is opt-in per deployment
  (`BIOAGENT_LLM_KEY_ENCRYPTION=1`, master key in its own 0600 file, never in the world-readable
  prod `.env`) and recorded **per row**, so it can be switched on later without a migration.
  **added** `src/bioagent/gateway/llm_providers.py` — OpenAI-compatible endpoint presets
  (OpenRouter / OpenAI / DeepSeek / DashScope / Moonshot / Gemini-compat / custom) + key
  verification. Split from the store so neither half does the other's I/O. Model ids are NOT
  hard-coded — providers retire them faster than we redeploy, so ids come from the live
  `GET /models`. `verify()` distinguishes the four causes of "my key doesn't work" (bad key /
  no credit / wrong model id / unreachable endpoint) because only one of them is fixed by
  getting a new key.
  **added** `tests/test_llm_credentials.py`, `tests/test_llm_providers.py`,
  `tests/test_llm_credential_routes.py`, `tests/test_llm_endpoint_binding.py` — 79 offline tests,
  no network. The load-bearing ones pin verify-before-commit (a failed rotation leaves the old key
  working) and bind-time key snapshotting (rotating mid-run does not break a running analysis).
  **added** `docs/byo_api_key_and_hpc_shell.md` — the locked decisions for both halves of this
  line, including the parts NOT yet built (CPU worker allocation, HPC3 shell toolset, HITL
  triggers) so the design survives even if the code lands later.
  **edited** `.gitignore` — added `ssh_creds/` and `llm_creds/`. `BIOAGENT_STATE_DIR` defaults to
  `"."`, so running the console from a checkout drops real PRIVATE SSH KEYS and API keys into the
  repo root as untracked files. The `ssh_creds/` hole PREDATES this work; found while adding the
  parallel store.
  **added** `src/bioagent/gateway/worker.py` — the session's standing CPU allocation, held so the
  agent's shell has somewhere RCIC-legal to run. `srun --jobid --overlap` per command (no queue
  wait); reuses `acquire_allocation`/`JobStore` from `slurm_job.py`. Opt-in
  (`BIOAGENT_WORKER_NODE=1`). Connect skips the GPU entirely when the user brings an API key.
  **added** `src/bioagent/tools/hpc_shell.py` — list_dir/stat_path/find_files/read_text/disk_usage
  on the LOGIN node (metadata-class, no allocation); run_shell/fetch_url/install_package on the
  WORKER. No command allowlist: the general shell simply never runs on a login node, so RCIC
  compliance is structural. Reads/writes confined (writes narrower than reads — the lab account is
  shared), re-checked after `readlink -f`. Crossing a line raises HITL, refusing by default when
  no approver is wired.
  **added** `src/bioagent/gateway/package_cache.py` — the lab-SHARED install cache
  (`<shared_root>/pkgs`), so a package one member installs is instantly there for everyone and is
  never downloaded twice. Immutable + atomically published (`mv -T` is the concurrency arbiter, no
  lock file) because on HPC3 one user cannot overwrite another's files — the same constraint that
  forced `hpc_gc.SHARED_SUBDIRS` to be per-user. Cannot shadow the image: publishing an existing
  module is refused, colliding deps are pruned, and `sys.path` is APPENDED via a generated
  `sitecustomize` (plain PYTHONPATH sorts before site-packages and would hijack — proven by an
  executed counterfactual test).
  **added** `tests/test_worker_node.py`, `tests/test_hpc_shell.py`, `tests/test_hpc_shell_wiring.py`,
  `tests/test_package_cache.py`.
  **edited** `gateway/slurm_sandbox.py` (binds the cache read-only into run_code),
  `agents/registry.py` (`hpc_shell` catalog), `gateway/settings.py` (worker_* knobs),
  `gateway/app.py` (worker lifecycle, `/api/confirm`, `RunState.confirm_event`, API-only connect).
  **added** `src/bioagent/agents/code_imports.py` + `src/bioagent/gateway/code_preflight.py` —
  a snippet's third-party imports are read from its AST BEFORE it runs, and anything missing is
  resolved in ONE confirmation. Replaces a path that was measured to be actively misleading: pip
  inside the sandbox could report success while the very next import failed (`--containall` leaves
  `~/.local` off `sys.path`), and nothing survived to the next step. The run_code tool description
  that invited it was removed. `tests/test_code_preflight.py`.
  The package cache was subsequently **measured on the real HPC3** (probe jobs, since cleaned up):
  the `--target`+atomic-publish design works there, and the Singularity-overlay alternative does
  NOT — no fakeroot, non-root cannot write image `site-packages`, and a writer blocks all readers.
  `<shared_root>` turned out to be already `drwxrwsr-x ruic20_hpc`, so that flagged risk was
  unreal. Details in the doc. Still unverified: `srun --jobid --overlap` and CPU queue latency.

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
- 2026-08-06 · `claude` · (worktree `vcf-normalization-variants-cef98c`)
  **added** `tests/test_ssh_transfer_host.py` — pins the control-plane / data-plane split in
  `SSHExecutor`. RCIC's 2026-08-06 notice reserves the HPC3 login nodes for logins and Slurm
  submission — no compute and no `rsync`/`SFTP`/`rclone`/`wget` — and we were pushing every
  upload (GB-scale VCFs, h5ad) over the login session. `put_file`/`get_file` now open their own
  connection to `BIOAGENT_HPC_TRANSFER_HOST` (`access-hpc3.rcic.uci.edu`) while `exec`, Slurm
  and tunnels stay on the login node; the tests assert WHICH connection each byte rides, that
  the parent `mkdir` stays on the control plane, and that an unusable transfer host degrades
  with one warning instead of breaking a run. No new source file — the change itself lives in
  `src/bioagent/gateway/{ssh_gateway,settings,app}.py`.

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

- 2026-07-28 · `claude` · (branch `main`) **added** `docs/decks/` —
  `AiScientist_Technical_Spec_EN.pptx`, `AiScientist_技术规格说明_中文.pptx`, and the
  `build_deck.js` generator that produces both from one source (a `t(en, zh)` helper keeps the two
  language versions structurally identical, so a content edit lands in both). A 27-slide technical
  specification deck requested by Yijun: job state design, context management (research vs fast-chat
  paths), cross-server job submission, the existing workflows / business capability, package selection
  and its rationale, positioning vs cloud science-agent platforms, and a six-slide deep dive on the
  in-development genetic-variant-annotation line. Regenerate with
  `node docs/decks/build_deck.js <en.pptx> <zh.pptx>` (needs `npm i pptxgenjs`). The `.pptx` files are
  build OUTPUT — regenerate rather than hand-edit, or the generator and the deck drift apart.

- 2026-07-31 · `claude` · (worktree `mmfatlas-service-setup-7ca4e1`, branch
  `claude/mmfatlas-service-setup-7ca4e1`) **added** `deploy/mmfatlas-service.md` — operational
  README for the MMFAtlas/CELLxGENE service, written after its second outage in a month
  (503 for ~22.5 h; the 2026-07-02 `targetPort` 5006 patch went stale when the container was
  SIGKILLed and restarted on its default 5005). Exists mainly to stop the recurring wrong-layer
  debugging: MMFAtlas is a k8s Deployment in its **own `mmfatlas` namespace** pulled by
  RKE2/containerd, so `docker ps` and the host `mmfatlas` service account will never show it.
  Complements `deploy/public-domain-tls.md` (TLS/cert side). MMFAtlas is Texera's service, not
  ours — the doc is a handoff to Jin, not a claim of ownership.

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

## 2026-09-02 — claude
- **added** `experiments/depth_matched_validation/result-2026-09-02.log` — ground-truth control re-run after the `run_depth_matched_de` selection/robustness/direction fix (main `5ee2b3a`+). The 2026-08-20 log is kept beside it; the README now carries both results, newest first. Committed.
- **added** `docs/discriminating-hypotheses.md` — design note for the `feat/discriminating-hypotheses` line: why the hypothesis ledger could only confirm, and the rival / gate / contest-adjudication change. Committed on that branch, not on main.
