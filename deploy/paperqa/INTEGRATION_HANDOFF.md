# deep_literature (PaperQA) — research-pipeline integration handoff

> Also usable as the PR description. Companion docs: `HOW_PAPERQA_WORKS.md`, `RUNBOOK.md`, `README.md`.

## What this adds

The research pipeline's **literature-grounding step now runs `deep_literature`** (PaperQA2 over the
lab's curated PubMedBERT corpus on HPC3) instead of the online Europe PMC `literature_search`.
Answers are grounded in **our own corpus** with real, in-corpus citations — no online preprints,
no hallucinated references.

## How it flows

```
research pipeline (research_lab)
  └─ literature step ──routed to──▶ deep_literature
       └─ gateway _build_literature_executor submits paperqa_cli inside paperqa.sif on HPC3 (Slurm)
            ├─ loads the OFFLINE PubMedBERT embedding from <root>/hf_cache
            ├─ REUSES the pre-built index under <root>/index_pubmedbert (no re-embed)
            ├─ reaches the session's Qwen at the GPU node's host:port
            └─ returns a grounded, CITED answer → References built from the corpus contexts
```

`literature_search` (Europe PMC) is kept as a **fallback** for dev / no-HPC runs, so behaviour
never regresses where deep_literature can't run.

## Code changed

- `agents/research_lab.py` — route the literature step to `deep_literature`
  (`_run_deep_literature_step`); References built from the corpus contexts. `literature_search`
  kept as fallback.
- `agents/registry.py` — expose + route `deep_literature` in the scientist and quickchat catalogs.
- `gateway/app.py` — `_build_literature_executor` submits `paperqa_cli` on HPC3; **`export HF_HOME`**
  to the offline model cache before Python starts; bind the index dir **read-write** (PaperQA's
  `answers` index must write a lockfile).
- `gateway/slurm_analysis.py` — add `extra_rw_binds` (read-write bind mounts).
- `tools/paperqa_search.py` — match the build-time `IndexSettings` / `ParsingSettings` so PaperQA
  **reuses** the pre-built index instead of rebuilding an empty one.
- `tools/paperqa_cli.py` — new HPC3 CLI entrypoint; point HF at the offline cache.
  **NOTE:** do *not* set `SENTENCE_TRANSFORMERS_HOME` — it makes sentence-transformers use its old
  cache layout and breaks offline model loading (this cost a full debug session).
- `deploy/paperqa/` — container def, build/stage script, runbook, docs.

## To enable (opt-in — gateway `.env`)

The feature is **OFF** unless all of these are set (guarded, so absence = zero behaviour change):

```sh
BIOAGENT_PAPERQA_ON_HPC=1
BIOAGENT_PAPERQA_IMAGE=/dfs3b/ruic20_lab/<user>/BioAgentPrototype/deploy/paperqa/paperqa.sif
BIOAGENT_PAPERQA_ROOT=/dfs3b/ruic20_lab/<user>/retigene
BIOAGENT_PAPERQA_PAPERS=/dfs3b/ruic20_lab/<user>/retigene/papers
BIOAGENT_PAPERQA_INDEX_DIR=/dfs3b/ruic20_lab/<user>/retigene/index_pubmedbert
BIOAGENT_PAPERQA_INDEX_NAME=retigene_full_pubmedbert
BIOAGENT_PAPERQA_MANIFEST=/dfs3b/ruic20_lab/<user>/retigene/paperqa_manifest.csv
BIOAGENT_PAPERQA_EMBEDDING=st-NeuML/pubmedbert-base-embeddings
```

Runtime guard also requires a live HPC3 session with the GPU/Qwen up (`conn.alloc`), non-mock.

### HPC3 prerequisites (one-time, per corpus owner)

1. Build + stage the container: `deploy/paperqa/build_and_stage.sh` → `paperqa.sif`.
2. Pre-stage the PubMedBERT model into `<root>/hf_cache` on a login node (has internet), then the
   compute nodes read it offline. See `RUNBOOK.md` step A3.
3. Build the index: `sbatch deploy/paperqa/embed_corpus.slurm` (≈1739 PDFs → `index_pubmedbert`).

### Retrieval breadth (optional overrides)

The RAG funnel is `paper_search` (pick candidate papers) → `gather_evidence` (summarize chunks) →
`gen_answer` (synthesize). PaperQA's stock gates are narrow (`search_count=8`, `evidence_k=10`,
`answer_max_sources=5`), which truncates reverse phenotype→gene questions whose correct answer is
spread across a dozen papers, and makes repeat runs of the same question return different genes
(a near-tied score band gets cut at 5). We now ship wider defaults, all overridable from the
gateway `.env`:

```sh
BIOAGENT_PAPERQA_SEARCH_COUNT=40      # candidate documents pulled from the index (default 40)
BIOAGENT_PAPERQA_EVIDENCE_K=40        # chunks summarized as evidence (default 40)
BIOAGENT_PAPERQA_MAX_SOURCES=20       # sources allowed into the final answer (default 20)
BIOAGENT_PAPERQA_ANSWER_LENGTH="about 500 words. When the answer is a list of genes, rank them by how many independent papers in the context support each one: give at most 10 as the primary list, strongest evidence first, and put everything else under 'also reported'. Every gene symbol you write must appear verbatim in the cited context - never infer, expand or abbreviate a symbol."
BIOAGENT_PAPERQA_CONCURRENCY=12       # parallel evidence summarization calls (default 12)
BIOAGENT_PAPERQA_TEMPERATURE=0.0      # 0.0 = reproducible across runs
```

Cost of the wider setting: a query goes from ~30–40 s to ~90–120 s, because `evidence_k` chunks each
cost one LLM summarization call. `BIOAGENT_PAPERQA_CONCURRENCY` is what keeps that from scaling
linearly — raise it if the Qwen server has headroom, lower it if you see vLLM queueing. The Slurm
wall clock (`run_code_time_limit=01:00:00`, auto `run_timeout_s = slurm_time + 300 s`) already
covers this; no change needed.

To go back to upstream behaviour, set `SEARCH_COUNT=8`, `EVIDENCE_K=10`, `MAX_SOURCES=5`.

These travel into the container as injected CLI args (`_build_literature_executor` →
`inject_args` → `_ARG_TO_ENV` in `paperqa_cli.py` → `os.environ`), because `apptainer --containall`
does **not** inherit the host environment. Adding a new knob means touching all three places.

## Status

Deployed and verified on the eyeserver console (aiscientist). Three unrelated questions returned
`status: ok` with citations drawn from the local corpus (classic papers, real authors/DOIs):

- CRB1 / retinitis pigmentosa → Khan 2014, Huang, Hanein, Bernal 2003, Zenteno 2011
- ABCA4 / Stargardt → Cremers 2020, Molday 2010
- RPE65 / LCA → Dev Borman 2012, Farrar 2017, Bowne 2011, Hull 2016

## Known follow-ups (non-blocking)

- **PI over-planning:** for a pure literature question the PI still tends to plan a full single-cell
  pipeline (QC/cluster/DE/enrichment) when it sees a gene name. Refining the plan to the one
  literature step works today; a cleaner fix is a PI-prompt tweak.
- **Answers index locking:** the PaperQA `answers` index is bound rw at the shared corpus path — fine
  for a single user; for concurrent sessions consider a per-session answers dir to avoid lock
  contention.
- **Semantic mismatch:** a question phrased as "macular atrophy" does not lexically match a paper
  titled "Stargardt disease". Widening `evidence_k` does not fix this — it needs phenotype-synonym
  query expansion before `paper_search`.
- **Citation numbering:** the model occasionally emits an inline `[3]` with no matching entry in the
  source list. This is a content-layer error, not fixable in the display-layer normalizer.
