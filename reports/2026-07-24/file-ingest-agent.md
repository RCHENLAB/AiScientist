# File ingest / triage agent (feature ①)

Branch `feat/file-ingest-agent` (off `main` f857a06). A general, LLM-driven "skim any uploaded file
and get the gist" step that replaces suffix-guessing with **content** understanding — Rui's
requirement: *"no matter what the file is, the agent should first skim it and get the gist."* This
is feature ① only; multi-file / bind-set (feature ②) is deliberately untouched.

## What was built

`src/bioagent/tools/dataset_inspect.py` — in-process, gateway/paramiko-decoupled (mirrors
`tools/hpo_terms/mapper.py`, so it unit-tests on a bare checkout). Same split the HPO mapper uses:
**code owns facts, the LLM does language.**

| API | What it does |
|---|---|
| `peek_dataset(path, *, max_bytes=262144, size_hint=None, name=None) -> dict` | Deterministic, **never-raises** peek of a bounded file head. Magic bytes; size; format-aware evidence where cheap — **VCF** header → assembly (explicit `assembly=`/`##reference`, else chr1 contig length) / sample id(s) / caller / chr-prefix / bgzip?; **HDF5** (`.h5ad`/`.h5`/`.loom`) group+dataset tree via `h5py` *without loading matrices*; **tabular** (`.csv`/`.tsv`) delimiter + columns + a few rows; **`.gz`/bgzip** decompress just the head (multi-member + truncated tolerant); otherwise first N text lines or a hexdump. Unknown/corrupt/binary → best-effort descriptor + note, never an exception. |
| `describe_dataset(peek, *, chat_fn) -> dict` | LLM step (injected `chat_fn`). Returns `{file_kind, format, assembly?, sample_ids?, likely_modality, key_facts[], one_line_summary, confidence, source}`. The model may only name/summarise the evidence — told to answer `unknown`/null rather than invent. Deterministic facts (assembly, sample ids, format) are **stamped back over** the model's answer so a description can never contradict the bytes. `chat_fn=None` or an unusable/failed reply → deterministic fallback built from the peek (`source:"deterministic"`). |
| `inspect_dataset(path, *, chat_fn=None, ...) -> dict` | peek + describe in one call → `{status, peek, description, raw_data_to_llm}`. |
| `make_inspect_dataset_tool() -> HarnessTool` | The `inspect_dataset` Scientist tool, mapper/registry pattern. Reaches the session model in-process via `ctx.tunnel_port` with **`think=False`** (load-bearing: a bounded call to the served reasoning model can spend its whole budget on the thinking trace and return empty content). Reads `args["path"]` or the run's bound `decisions["dataset_path"]`. |

## The upload-time / no-GPU decision

At upload there is usually no served model, and bringing one up costs ~10 min and an A100. So the two
halves run at **different times on purpose**:

> *Superseded detail (2026-07-27):* this section originally justified the split by lazy GPU
> provisioning (`BIOAGENT_LAZY_GPU`), which has since been removed — `/api/connect` now brings SSH
> and the GPU up together. The split itself stands unchanged: an upload must never depend on, or
> trigger, a served model.

- **PEEK runs synchronously at upload** — `/api/upload` (single-file) and the `/api/upload/chunk`
  finalize — on the still-local file **before** it is staged to dfs3b (staging deletes the local
  copy). Deterministic, instant, no model. The one-line deterministic gist is appended to the
  existing upload success toast, and the full peek rides back on the upload response (`"peek"`).
- **DESCRIBE runs only when a model already exists.** New endpoint **`POST /api/dataset/describe`**
  (`{connection_id, path, name?}`) peeks (local, or a **bounded base64 head** of a remote dfs3b file
  — never pulls a whole WGS VCF back) and, **only if `_vllm_reachable(conn)`**, runs the LLM
  description. If no model is up it returns the deterministic peek + a "start the GPU" note. It
  **never** provisions a GPU — upload/triage must not trigger the spin-up.

## Wired vs deferred

**Wired**
- `peek_dataset` at both upload paths (single + chunked), before HPC staging; peek on the response + gist in the toast.
- `POST /api/dataset/describe` on-demand endpoint (local + remote-head, model-gated, non-provisioning).
- `inspect_dataset` registered in `agents/registry.py` → present in `build_scientist_catalog()` (so the orchestrator can skim its bound dataset mid-run). **Not** added to `build_quickchat_catalog()`.
- `tests/test_dataset_inspect.py` — 24 offline tests (scripted `chat_fn`, no `gateway.app` import).

**Deferred (out of scope for this PR)**
- Suffix routing is **augmented, not replaced**: `_primary_suffix`/`_primary_rank`/`select_pipeline` still pick a folder's primary file. Letting the LLM triage *drive* pipeline selection is a follow-up.
- No auto-describe at upload (GPU cold) and no describe-at-run-start hook yet — the endpoint + the in-run tool are the two live surfaces. A "describe at run start when the GPU is already warm" hook in `_run_lab` is a candidate next step.
- No frontend UI beyond the enriched toast + the `peek` field on the response; a Datasets-panel "what is this?" affordance calling `/api/dataset/describe` is left for the frontend line.
- Multi-file / bind-set is feature ② — untouched.

## Open questions for Yijun

1. **Should content triage eventually override suffix routing?** Today a `.txt` that is actually a VCF still routes by suffix. The peek already knows better (`detected_format`); wiring that into `select_pipeline` is a real behaviour change and needs your call.
2. **Auto-describe at run start?** When a run begins the GPU is (being) provisioned anyway — that is the natural free moment to attach an LLM description to the run bundle. Worth a small `_run_lab` hook, or keep it strictly on-demand?
3. **Privacy posture of the sample to the LLM.** Describe sends a *bounded head* (≤256 KB, trimmed to ~4 KB of text) to the **session's own** served model — same trust boundary as `map_phenotype_to_hpo` sending the case note. Uploads otherwise send "only derived metrics to the LLM". Confirm this bounded-sample exception is acceptable (documented in the module header).
4. **`inspect_dataset` in the Scientist catalog** — it is now always present (one more tool schema per turn). If you would rather gate it behind a pipeline SKILL.md instead of the global catalog, easy to move.
