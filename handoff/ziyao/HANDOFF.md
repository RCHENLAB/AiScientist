# Handoff — RetiGene corpus recovery (literature line)

## 2026-07-16 — Re-downloaded 67 problem papers to journal (VoR) versions

Author: claude (Ziyao literature line)
Status: **64 of 67 recovered (62 journal VoR + 2 PMC VoR); 3 stragglers remain (list below).**

### What this was
Professor Li flagged (a) PMID 30905644 was only Figure 1, and (b) asked to replace PMC/author-manuscript
files with the publisher journal (Version of Record) where a DOI exists. Combined list = 67
(24 quality problems + 43 PMC→journal upgrades). Verified each via poppler (page count + first-page
content + extractable text) and recorded in the priority manifest with new `selected_source`/`selected_pdf`.

### Recovered (62) — by route
- ScienceDirect / Elsevier "click View PDF" (incl. old 10.1086 AJHG that resolve to Cell Press): 33
- Wiley pdfdirect (`onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true`): 8
- Nature/Springer content PDF (`link.springer.com/content/pdf/<DOI>.pdf`): 9
- Thieme (`thieme-connect.de/products/ejournals/pdf/<DOI>.pdf`): 3
- IOVS/ARVO (toolbar PDF button): 4
- BMJ J Med Genet (`jmg.bmj.com/content/jmedgenet/<vol>/<iss>/<page>.full.pdf`): 1
- PNAS, bioRxiv, T&F, DMM (Company of Biologists): 1 each

Manifest still shows **1741 selected** (these were in-place replacements, not new rows).

Also recovered via PMC (VoR scan / OA full text) after publisher routes failed:
- **1732158** Genetics 1992 — OUP paywalled → PMC1204789 PDF (11 p, real text).
- **23364359** Thromb Haemost — Thieme gated → PMC3641626 PDF (6 p, real text).

### Remaining 3 — see `output/retigene_papers/journal_priority/_refetch_remaining_3.csv`
- **9380435** Pediatr Res 1997 — Nature states "we don't have this article in PDF format" → journal PDF
  genuinely does not exist; only a 1-page placeholder. A stale 1-page stub is still in `papers_priority/`
  (sandbox couldn't delete it; remove on the Mac if desired).
- **26510000** Retin Cases Brief Rep (Ovid) — License Service Failure (E3), single-user slot persistently
  busy; recoverable later via Primo → Download PDF (reaches oce.ovid.com). **Retry.**
- **21686617** BMJ Case Rep 2009 — no PDF rendition anywhere (BMJ fellowship-gated; PMC/Europe PMC have
  HTML full text only). Would need HTML→PDF reconstruction.

### Next
- Regenerate `paperqa_manifest.csv` (file paths changed for the 62 re-downloads) before the HPC3 embed.
- Optionally retry 26510000 (Ovid) and 1732158 (PMC) later.

---

# Handoff — `deep_literature` (PaperQA2 over local Qwen)

> ⚠️ **Read the `## 2026-06-28` section at the very bottom-of-top FIRST** — it hands off the
> NEW **references module** + the finalized remote-provider decision (Edison/FutureHouse Crow),
> and lists what the literature line needs to do next. The 2026-06-19 section below it is the
> original `deep_literature` handoff (still accurate, and that tool stays UNTOUCHED).

---

## 2026-06-30 — References rewrite: no hidden final fallback search

Author: Codex (Ziyao literature line)
Status: **implemented locally; targeted tests pass.** This supersedes the earlier same-day
`gather_references()` fallback design below.

### Decision

The final report must not "silently rescue" missing literature work by running a second
Europe PMC search at report time. That made the report look successful even when the agent did
not actually execute a literature step. The new contract is:

- `literature_search.py` owns all Europe PMC retrieval, query focusing, and weak-hit filtering.
- `literature_references.py` only formats/inserts citations already produced by accepted
  `literature_search` steps.
- `gateway/app.py` no longer imports or calls `gather_references()`. If no accepted
  DOI/PMID-backed `literature_search` result exists, it inserts an honest empty References note
  and records that no hidden fallback search ran.
- `ResearchLab` now has a deterministic agenda guard: when the user asks for literature,
  references, citations, background, or biological interpretation and the PI omits a literature
  step, the guard adds/replaces a low-priority step with `Literature search ...`.
- A literature agenda step is still deterministically routed directly to `literature_search`;
  the LLM Scientist does not choose whether to call it.

### Changed Files

- `src/bioagent/tools/literature_search.py`
  - Added `focus_literature_query()`.
  - Moved dataset/file/metadata stopword cleanup and generic/off-topic result filtering here.
  - Query focusing now drops tool-instruction words such as `Search`, `Return`, `citations`, and
    `evidence`, so a prompt like `Search DDX41 retina Return citations evidence` focuses to
    `DDX41 retina`.
- `src/bioagent/tools/literature_references.py`
  - Rewritten as formatting-only: `references_from_citations()`, `empty_references()`,
    `format_references_section()`, `insert_references()`, `degradation_note()`.
  - Removed the report-time retrieval responsibility; no Europe PMC or remote-provider calls.
- `src/bioagent/gateway/app.py`
  - Final References now come only from `_references_from_accepted_literature_search(result)`.
  - If that returns `None`, final report records no accepted literature-search citations.
  - Report writer/reviewer prompts now forbid bibliography-style body blocks (`Title:`,
    `Authors:`, `DOI/PMID:`), and the finalization path removes those metadata lines from the
    manuscript body before re-inserting the authoritative `## References` section.
  - Literature-review prose now strips hallucinated figure callouts such as
    `(see Figures 1-2 for cited figure references)` while preserving normal Results figure
    references.
- `src/bioagent/agents/research_lab.py`
  - Added deterministic agenda repair for requested literature context.
  - Literature steps are still hard-routed to `literature_search`.
  - Duplicate literature agenda items are collapsed into one canonical `Literature search for ...`
    step, including the UI/model pattern `literature_search` plus a natural-language literature step.
- Tests
  - `tests/test_literature_search.py` now covers query focusing and Europe PMC hit filtering.
  - `tests/test_literature_references.py` now covers formatting/insertion only.
  - `tests/test_research_lab.py` covers the omitted-literature-step guard.

### Validation

- `PYTHONPATH=src python3 -m pytest tests/test_literature_search.py tests/test_literature_references.py tests/test_research_lab.py::test_pi_plan_guard_adds_literature_step_when_model_omits_it tests/test_research_lab.py::test_literature_context_step_is_deterministically_routed_to_literature_search tests/test_research_lab.py::test_literature_step_detection_does_not_match_background_rna_cleanup -q` → **19 passed**.
- `PYTHONPATH=src python3 -m pytest tests/test_literature_search.py tests/test_literature_references.py tests/test_research_lab.py::test_pi_plan_guard_adds_literature_step_when_model_omits_it tests/test_research_lab.py::test_pi_plan_guard_collapses_duplicate_literature_steps tests/test_research_lab.py::test_literature_context_step_is_deterministically_routed_to_literature_search tests/test_research_lab.py::test_literature_step_detection_does_not_match_background_rna_cleanup -q` → **21 passed** after the duplicate-plan fix.
- `PYTHONPYCACHEPREFIX=/tmp/bioagent_pycache PYTHONPATH=src python3 -m py_compile ...` on the changed
  Python files, including the report-body and literature-figure cleanup paths → **passed**.
- Gateway-specific tests still cannot run in this local interpreter because FastAPI is not installed;
  run the gateway suite in the project/test environment before merge.

### New Success Criterion

The literature feature is considered working only when the run actually calls and accepts
`literature_search`, and final `## References` reuses that step's DOI/PMID-backed citations.
The final report no longer proves literature success by doing a hidden fallback search.

---

## 2026-06-30 — Final References reuse accepted citations + planner now preserves literature steps

Author: Codex (Ziyao literature line)
Status: **implemented locally; targeted literature-reference tests pass.** Gateway app test was added
for the new bridge, but this local Python environment skips gateway tests because FastAPI is not
installed here. Syntax checks pass.

### What failed on HPC

HPC run `16ff701c516c` showed a new integration bug:

- The planned literature step ran and was accepted. The log shows three successful Europe PMC
  `literature_search` calls for focused queries such as `DDX41 retina innate immunity`,
  `DDX41 RIG-I MDA5 antiviral signaling`, and `DDX41 microglia retinal inflammation`.
- Those accepted step results included real DOI/PMID-backed citations such as Mars et al. on
  DDX41 retinal dystrophy and Sauter et al. on the retinal RLR antiviral system.
- The final manuscript still ended with:
  `No external citations were retrieved for this run (literature retrieval unavailable).`

Root cause: the final `## References` writer did **not** reuse accepted `literature_search` citations
from the lab run. It always called `gather_references(req.question)` again. For this dataset prompt,
the final query focus became `uploaded DDX41 DEG h5ad single-cell RNA-seq conditional knockout
wild-type mouse`, which is much worse than the accepted literature-step queries and can retrieve
nothing after filtering.

### What changed

- `src/bioagent/gateway/app.py`
  - Added `_references_from_accepted_literature_search()`.
  - Final report References now prefer accepted in-loop `literature_search` tool results.
  - If the scientist's accepted literature summary explicitly mentions DOI/PMID values, the final
    References section keeps only those cited papers, preventing raw broad Europe PMC hits from being
    dumped into the manuscript.
  - If no accepted DOI/PMID-backed literature-search citation exists, the old
    `gather_references(req.question)` fallback still runs.
  - Writes `process/literature_references.json` with the final literature result so future result
    bundles show exactly which path populated `## References`.
  - Emits a clearer success message when citations are reused from the accepted literature step.
- `src/bioagent/agents/research_lab.py`
  - Hardened the PI planning rule from optional to conditional-required: if the user asks for
    literature context, biological interpretation, background, references/citations, or a report
    grounded in published biology, the PI must reserve one agenda step for `literature_search`.
  - The prompt also tells the PI to combine/drop a lower-priority descriptive/annotation step if the
    5-step cap would otherwise crowd out the literature step.
  - Added deterministic execution routing for literature-context agenda steps. If a step contains
    literature/reference/citation/background wording, ResearchLab now directly executes the
    `literature_search` tool instead of asking the LLM Scientist to choose a tool. This prevents the
    model from calling literature search during an unrelated failed QC retry or skipping it in the
    actual literature step.
- `src/bioagent/tools/literature_references.py`
  - `degradation_note()` now treats any `status="ok"` result with citations as successful, including
    the new `tier="lab_literature_search"` path.
  - Added more dataset/file/metadata stopwords (`uploaded`, `h5ad`, `DEG`, `sampleid`, `majorclass`,
    `celltype`, etc.) so final fallback queries do not send UI/data-file terms to Europe PMC. The
    DDX41 dataset prompt now focuses to `DDX41 conditional knockout mouse retina`.
- `tests/test_gateway_lab.py`
  - Added regression coverage for the HPC failure mode: accepted `literature_search` citations are
    reused, rejected-round citations are ignored, and unmentioned broad hits are not inserted.
- `tests/test_literature_references.py`
  - Added coverage that reused accepted lab citations do not generate a false "NO citations" technical
    degradation note.
  - Added coverage for dataset prompt cleanup so `DDX41_DEG.h5ad`, `sampleid`, `majorclass`, and
    `celltype` do not leak into the final fallback literature query.
- `tests/test_research_lab.py`
  - Added coverage that the PI system prompt carries the conditional-required `literature_search`
    planning rule.
  - Added coverage that a `Summarize findings with literature context` step bypasses model tool
    selection and deterministically calls `literature_search`, returning a final answer containing
    the DOI needed by final References reuse.
  - Added a guard test that ordinary analysis wording such as `background RNA correction` does not
    trigger the literature route.

### Validation

- `PYTHONPATH=src python3 -m pytest tests/test_research_lab.py::test_literature_context_step_is_deterministically_routed_to_literature_search tests/test_research_lab.py::test_literature_step_detection_does_not_match_background_rna_cleanup tests/test_research_lab.py::test_pi_system_prompt_carries_design_aware_rules tests/test_literature_references.py -q` → **21 passed**.
- `PYTHONPYCACHEPREFIX=/tmp/bioagent_pycache PYTHONPATH=src python3 -m py_compile ...` on changed
  Python files → **passed**.
- Gateway-specific test could not run in this local interpreter because `fastapi` is missing; it should
  be run on the project/test environment before merge.

### Remaining caveat

This fixes final manuscript References wiring. It does **not** fix the unrelated repeated `run_code`
errors in the single-cell custom analysis step; that remains core/orchestrator analysis behavior.

---

## 2026-06-30 — Europe PMC fallback quality fix (query focus + abstract-book filtering)

Author: Codex (Ziyao literature line)
Status: **implemented locally and validated in the full UI pipeline.** Targeted tests passed
(`tests/test_literature_search.py`, `tests/test_literature_references.py`, and follow-up gateway
wiring in `tests/test_gateway_lab.py`). Crow/Edison remains out of scope for this change.

### What we learned

Europe PMC itself is implemented and wired into the report pipeline. A local direct call such as
`gather_references("What diseases are caused by germline DDX41 mutations?")` returns real DOI-backed
citations. A fresh local gateway run also proved the automatic report path is active: run
`b8271a7f6a7a` produced `report/technical_report.md`, recorded Europe PMC fallback, and inserted a
numbered `## References` section into `report.md`.

The remaining problem was **quality, not connectivity**. In the app, the automatic references module
was using the full user instruction as the Europe PMC query (for example, "Write a short
literature-only report ... Do not run QC ... Include real references"). Europe PMC is a keyword API,
so these UI/report instructions can pull broad conference abstract books above specific DDX41 papers.
The observed bad output included generic titles such as annual-meeting abstracts instead of focused
DDX41 disease reviews.

A later full dataset run exposed a second report-pipeline issue: the draft initially received good
automatic citations, but the LLM self-review/report rewrite could replace them with weak references
such as "Publication Only" or unrelated DOI-backed hits such as CAR T AML. The fix below makes the
automatic references module the final authority immediately before render.

### What changed

- `src/bioagent/tools/literature_references.py`
  - Added a deterministic `_focus_reference_query()` step before retrieval. It leaves short scientific
    questions alone, but converts report/task instructions into compact biomedical queries, e.g.
    `diseases caused germline DDX41 mutations`.
  - Added Europe PMC citation cleanup for the automatic References path: generic meeting/conference
    abstract books are filtered out, and retained citations are ranked toward DOI/PMID-backed hits
    whose title/metadata match query terms such as `DDX41`, `germline`, and `mutations`.
  - Tightened full-pipeline query cleanup so final-report instructions such as `Important`, `must`,
    `populated`, `weak placeholders`, `relevant biology`, and `invent` are not sent to Europe PMC.
    Example full-pipeline prompt now focuses to `DDX41 retina innate immunity hematopoiesis`.
  - Added off-topic filtering: DOI-backed citations are still dropped when title/journal/authors do
    not match any focused query term. This removed the recurrent CAR T AML citation from DDX41 retina
    reports.
  - Treats `Publication Only` as a generic/non-useful citation title and filters it.
  - `insert_references()` now removes stray non-H2 References sections (for example a model-made
    `### References`) before inserting the authoritative `## References`, preventing duplicate
    References blocks in final reports.
  - Added `query`, `unfiltered_count`, and `filtered_count` fields to `gather_references()` results so
    future debugging can see exactly what Europe PMC received and how many hits were dropped.
  - Extended `degradation_note()` so the technical report records the focused query and filtering
    count; no more guessing about what was searched.
- `tests/test_literature_references.py`
  - Added regression coverage that a long report instruction is focused before Europe PMC.
  - Added regression coverage that generic abstract books are filtered while a specific DDX41 paper is
    retained.
  - Added regression coverage for full-pipeline citation-instruction cleanup, off-topic DOI filtering,
    `Publication Only` filtering, and duplicate/stray References-section removal.
- `src/bioagent/gateway/app.py`
  - Follow-up full-pipeline fix: the report self-review step can still mutate or replace a correctly
    populated `## References` section. The gateway now re-applies `insert_references()` **after**
    self-review, making the references module the final writer of manuscript citations.
- `tests/test_gateway_lab.py`
  - Added regression coverage that a corrupting reviewer cannot leave weak/fabricated references in
    the final manuscript; the literature module's DOI-backed citations are restored before render.

### UI validation

- Full dataset run `fd2f94412a28` proved the self-review-after-reference bug was fixed enough to
  replace weak references with relevant DDX41/retina/immunity citations, but still showed one
  off-topic CAR T AML citation.
- After the off-topic filter and stray-References cleanup, full dataset run `498deb43051c` produced a
  single final `## References` section with two relevant DOI-backed citations:
  - Mars Z et al. (2026), biallelic germline `DDX41` variants causing retinal dystrophy /
    retinal homeostasis, DOI `10.64898/2026.01.28.26344834`.
  - Devasahayam Arokia Balaya R et al. (2025), DEAD/DEAH-box helicases in immunity, infection and
    cancers, DOI `10.1186/s12964-025-02225-9`.
  - No `Publication Only`, no CAR T AML, no duplicate `### References` block.

### Important boundaries

- This only changes the **automatic manuscript References module** (`literature_references`). It does
  **not** change the lower-level `literature_search` Scientist tool, and it does **not** touch
  `deep_literature`/PaperQA2.
- Crow/Edison is still ignored for now unless `BIOAGENT_LITERATURE_REMOTE_URL` is explicitly set.
- This improves Europe PMC fallback quality, but Europe PMC is still keyword metadata search, not
  grounded full-text synthesis. Mode B/front-loaded evidence remains future work.

### Remaining caveats

- This fixes final manuscript References, not the correctness of the single-cell DE/statistical
  analysis itself. Data-analysis behavior remains owned by the core/orchestrator line.
- Europe PMC fallback is still keyword metadata search, not full-text RAG; for richer evidence
  synthesis, Tier 1 Crow/Edison or Mode B/front-loaded literature remains future work.
- A true literature-only preset would still improve planning UX, but the final report References no
  longer depend on the planner remembering to include a literature step.

---

## 2026-06-28 — references module + remote-provider decision (handoff to Ziyao Ma)

Author: claude (on Yijun's core line) — written FOR the literature line at Yijun's request.
Status: **references module LANDED on `main`** (commits `1de5ec6`, `ada3e8d`); the remote Tier-1
integration + Mode-B are the open work for you.

### Why you're getting this

The PI manuscript writer always reserved a `## References` section with the placeholder
*"Citations to be inserted by the literature module (PaperQA)."* — **but nothing ever filled it.**
I built that missing module and wired it into the report pipeline. The retrieval *backend* choices
and the next feature (front-loading literature into writing) are literature-line work — hence this
handoff. **Your existing `deep_literature` tool was deliberately left untouched.**

### What landed (already on `main`)

- **`src/bioagent/tools/literature_references.py`** (new) — fills the manuscript's `## References`
  slot with REAL citations. Tiered, never fabricates, never raises (same graceful-degrade contract
  as `literature_search`). Public exports: `gather_references()`, `format_references_section()`,
  `insert_references()`, `degradation_note()`.
- **`src/bioagent/gateway/app.py`** (wired) — after the draft is written, fills references *before*
  self-review; threads a `degradation_note` into `_build_technical_report` so any fallback is
  recorded in the **technical report's "Diagnostics & failures"** section. The **academic
  manuscript renders normally** regardless. Writer/reviewer prompts updated to preserve the
  now-filled References verbatim.
- **`tests/test_literature_references.py`** — 11 tests (tier selection, privacy, insertion,
  honest-empty, degradation note). Full suite 193 passed.
- **`docs/archive/literature_embedding_plan.md`** (new) — the finalized plan; read it, it's the source of
  truth (supersedes the old WeChat "选型清单").

### The two tiers — and the honest answer to "are BOTH external? I thought we'd grep"

**Yes — as shipped, BOTH tiers are external network services. Neither is local grep.**

| Tier | What | External? | Embedding |
|---|---|---|---|
| **1 (primary)** | **FutureHouse / Edison Scientific platform, agent Crow** (PaperQA2-based) | ☁️ external cloud | provider-side |
| **2 (fallback)** | **Europe PMC REST API** (`literature_search.search_europepmc`) | ☁️ external (ebi.ac.uk) — keyword API, **no embedding** | none |

**Why Tier 2 is Europe PMC and not local grep:** grep needs a *local corpus to grep over*, and we
don't maintain one. Europe PMC is the lightweight, no-embedding, real-DOI/PMID, biomedical keyword
fallback that needs no index. It only fires when Tier 1 is down **but the host still has internet**
— which was the agreed scope (a fully-offline UCI box can't reach *either* service, and we agreed
not to handle full-offline).

**⟶ OPEN DECISION (Yijun is reconsidering this): add a truly-local Tier 3 = grep over a cached
paper corpus?** It would only fire when *both* external services are unreachable (i.e. genuinely
offline). It requires maintaining a local library of papers/abstracts on HPC3 to grep. Yijun's
earlier call was "don't handle offline" → no grep. If that changes, the work is: (a) decide what
the local corpus is + where it lives, (b) add a `_gather_grep()` tier below Europe PMC returning
the same citation shape. **Flagging for the literature line to weigh in.**

### Open items for the literature line (Tier 1 integration)

The remote tier is **env-gated and currently dormant** → in practice the system runs on the
Europe PMC fallback until you wire Tier 1. To turn Crow on:

1. **Get an Edison/FutureHouse API key** and **confirm their data-retention / "is my query used for
   training" policy** — compliance gate. (We only ever send the *public research question*, never
   the synthesis; confirmed OK with JinLi. Still verify the provider side.)
2. Pick the integration:
   - **Fast path (works today):** stand up a thin REST wrapper around `edison-client` (was
     `futurehouse-client`) that accepts `{query, top_k}` and returns `{answer, references:[...]}`,
     then set `BIOAGENT_LITERATURE_REMOTE_URL` (+ `BIOAGENT_LITERATURE_REMOTE_KEY`). The generic
     remote path in `literature_references.py` already speaks this shape.
   - **Native path (stabler):** once you have a key and can see the live response object, add a
     native `edison-client` adapter (lazy import + graceful degrade, mirroring `paperqa_search.py`).
     I did **not** hard-bind the client — couldn't verify its response schema without a key.
3. With either set, the primary tier auto-switches from Europe PMC → Crow; the `degradation_note`
   plumbing then only fires on real outages.

### Cost & rate limits (cost is now a stated priority — see `COST_AND_CACHING.md`)

- **Europe PMC (Tier 2) = effectively free.** No key, no paywall (we only take metadata/DOI), no
  charge. Rate limit is EBI fair-use (soft 429 under heavy concurrency); our ~few-queries-per-report
  volume never approaches it. So the fallback can never produce a surprise bill.
- **Crow (Tier 1) = credit-metered.** Buy credits → API key → each call spends credits; "generous
  free tier", Crow cheaper than Falcon. ⚠️ **Exact $/credit + free-tier limits are UNCONFIRMED**
  (pricing/FAQ pages 404'd) — log into `platform.edisonscientific.com` to get live numbers before
  budgeting.
- **Cost levers:** use Crow not Falcon; Europe PMC first / Crow on-demand; **a local
  question→answer cache to avoid re-paying for repeat queries** (this is the genuinely useful "local"
  component — saves money, not an offline grep); cap Crow calls per report.
- Full breakdown + the report you can forward: **`handoff/ziyao/COST_AND_CACHING.md`**.

### Next feature Yijun wants (NOT built — needs a green light)

**Mode B — front-load retrieval into writing:** let the model query literature *while* writing
**Introduction + Discussion** and cite from it, so References become a byproduct of what was
actually used (today it's "末端引用器" — write first, cite last). Plan in
`docs/archive/literature_embedding_plan.md` §4. It's a `_REPORT_WRITER_SYSTEM` orchestration change in
`app.py`; **Results/Methods stay literature-free.** Not started.

### Pointers
- Plan + decision: `docs/archive/literature_embedding_plan.md`
- Module: `src/bioagent/tools/literature_references.py`
- Wiring seam: `app.py` `_run_lab` (references fill) + `_build_technical_report` (degradation note)
- Your untouched tool: `src/bioagent/tools/paperqa_search.py` (see the 2026-06-19 section below)

---

# (original) Handoff — `deep_literature` (PaperQA2 over local Qwen)

Date: 2026-06-19
Author: ziyao (literature line)
Status: **code complete, mock-tested locally; real-Qwen end-to-end NOT yet verified** (needs HPC3 — see Open Items).

## TL;DR

Added a new Scientist tool, `deep_literature`, that wraps **PaperQA2** (the open-source
engine behind Edison Literature, github.com/Future-House/paper-qa). Where the existing
`literature_search` (Europe PMC) returns a *list of papers*, this returns a *grounded,
cited answer* (PaperQA's RAG loop: search → gather evidence → synthesize with in-text
citations).

PaperQA defaults to OpenAI for both its LLM and its embeddings. **The whole point of this
work was to repoint all of that at our local stack** so nothing leaves UCI: the LLM →
this run's local Qwen vLLM endpoint, the embedding → a local sentence-transformers model.
Only PaperQA's public bibliographic search touches the network.

## What was built

Four files (PR-ready):

- `src/bioagent/tools/paperqa_search.py` — **new**. The tool + the local-model wiring.
- `src/bioagent/agents/registry.py` — registers `make_paperqa_tool()` in the catalog.
- `pyproject.toml` — adds the optional `literature = ["paper-qa[local]"]` extra.
- `tests/test_paperqa_search.py` — **new**. 7 mock tests (no paper-qa install needed).

## Design (matches the existing tool conventions)

- **Tool shape.** `make_paperqa_tool()` returns a `HarnessTool` named `deep_literature`,
  `category="literature"`, `reads_private_data=False`. Same self-describing contract as
  every other tool, so the registry / System page pick it up automatically.
- **Local model wiring (the core).** `_local_endpoint(ctx)` reads `ctx.tunnel_port` and
  `ctx.model` — the same per-run context fields the live `chat_tools` path uses — and
  builds `http://127.0.0.1:{tunnel_port}/v1`. `_build_settings(ctx)` feeds that into a
  PaperQA `Settings` via a LiteLLM `model_list`, pinning `llm` / `summary_llm` /
  `agent_llm` all to local Qwen, and `embedding` to a local sentence-transformers model
  (`st-` prefix, default `st-multi-qa-MiniLM-L6-cos-v1`, overridable via env).
  > NOTE: an earlier draft read `ctx.ollama_port`, which does not exist on
  > `HarnessContext` — so it always returned "unavailable" and never reached the model.
  > Fixed to `ctx.tunnel_port`. This is the one bug to be aware of if comparing to drafts.
- **Graceful degradation.** `paper-qa` is heavy, so it is imported lazily. If it is not
  installed → `status="dependency_missing"` (same contract as `scrna_pack` for scanpy);
  if there is no local endpoint → `status="unavailable"`; any PaperQA failure is caught
  and returned as `status="error"`. The tool never raises, so it cannot kill a run.
- **Env knobs (no hard-coding of server-specific paths):**
  - `BIOAGENT_PAPERQA_PAPERS` — directory of PDFs PaperQA reads (defaults to
    `<workspace>/papers`).
  - `BIOAGENT_PAPERQA_EMBEDDING` — local embedding model name.
  - `BIOAGENT_LLM_API_KEY` — placeholder bearer for the local vLLM (`sk-no-key-required`).

## Privacy (the hard requirement)

- LLM (general / summary / agent) → local Qwen over the session tunnel. No prompt text to
  a cloud model.
- Embedding → local sentence-transformers, in-process. No chunk text to a cloud embedding
  API.
- Network is touched **only** by PaperQA's metadata/search clients (Crossref / Semantic
  Scholar / Unpaywall) with public bibliographic queries — never the dataset.
- `tests/test_paperqa_search.py::test_success_pins_models_to_local_endpoint` is a
  regression guard: it asserts the LLM `api_base` is `127.0.0.1` and the embedding is a
  local `st-` model. If someone repoints these at a cloud model, that test goes red.

## Tests — what they do and DON'T cover

The mock tests fake `paperqa` via `sys.modules` (the same idea as `test_literature_search`
mocking `httpx.get`), so the suite runs locally with no paper-qa install, no network, no
GPU — a "mock Qwen". `python3 -m pytest tests/test_paperqa_search.py -v` → 7 passed.

They cover the **plumbing**: empty-question error, `dependency_missing` path,
`unavailable` without a tunnel, the cited-answer parser, the local-endpoint/embedding
pinning, error-is-not-fatal, and the tool self-describing.

They **do NOT** cover answer quality, nor that the parser matches the *real* PaperQA
response object (the fake response shape in the test is an assumption), nor the exact
LiteLLM model string against the live vLLM. **Those can only be confirmed with real Qwen.**

## Open Items (the real-Qwen integration — needs HPC3)

1. **Install on the server.** `paper-qa[local]` is declared in `pyproject.toml` but not yet
   installed in `/data/BioAgent/env`. Confirm `pip install paper-qa[local]` resolves
   cleanly there (it pulls torch + sentence-transformers).
2. **PDF corpus.** Decide where the lab's PDFs live and set `BIOAGENT_PAPERQA_PAPERS`.
   Without papers, PaperQA's search is limited.
3. **Model-name alignment.** The deployed model is `QuantTrio/Qwen3.6-35B-A3B-AWQ`
   (`BIOAGENT_VLLM_MODEL`). Confirm the LiteLLM model string (`openai/<ctx.model>`) matches
   what vLLM serves (`--served-model-name`); adjust if needed.
4. **End-to-end run + audit.** Run a real research task so the gateway provisions Qwen on
   HPC3 + opens the tunnel, and confirm `deep_literature` returns a grounded cited answer.
   Use PaperQA `verbosity=3` to log every LLM/embedding call and **audit that nothing hits
   a cloud API**.
5. **Response-parser check.** Verify `_extract_answer` against the real PaperQA response
   (field names may differ by paper-qa version); adjust the getattr fallbacks if so.

## How it plugs in (for reviewers)

No new connection code is needed: `deep_literature` consumes the existing per-run context
(`ctx.tunnel_port` / `ctx.model`) that the harness already provides to every tool, exactly
like `scrna_pack` and `literature_search`. Once a run starts and the Qwen tunnel is up, the
tool reaches the model automatically.
