# Handoff → the literature line (Ziyao)

**From:** the core/orchestrator line (`handoff/yijun/`). **Date:** 2026-07-17.
**Why this is here and not in `handoff/ziyao/`:** `CLAUDE.md` says each line owns its own handoff and
must not edit another's — so this is written as a dated report for you to read and fold into
`handoff/ziyao/` however you see fit.

**Not a PaperQA2 tutorial.** You are ahead of me there (`handoff/ziyao/HANDOFF.md`,
`src/bioagent/tools/paperqa_search.py`). This is the set of things **on the orchestrator side** that
will affect your integration — one of which will silently break you if nobody tells you.

---

## 1. ⚠️ Read this first — the trap that cost a full day

**Any bounded LLM call you make against the served Qwen3.6 can return an EMPTY string, silently, and
look like "the model found nothing".**

The served model is a **reasoning** model. `vllm_client.complete()` does not disable thinking, so the
reasoning trace is generated *before* the answer and **counts against `max_tokens`**. If the budget runs
out mid-thought you get `finish_reason=length` and **empty `content`** — no exception, no error log.

This is exactly how `map_phenotype_to_hpo` returned 0 HPO terms in production while the case note was
correctly attached the whole time. Measured on the real model, same input:

| Config | Result |
|---|---|
| thinking ON, `max_tokens=800` | empty content → **0 terms** |
| thinking ON, `max_tokens=4000` | **still empty** → 0 terms |
| thinking OFF, `max_tokens=800` | correct output ✅ |

Default-effort thinking burned **4,529 reasoning tokens** on a 2,984-char note; the actual answer was
~520. **Raising `max_tokens` is not a reliable fix — disabling thinking is.**

**What you must do:** for any structured/JSON extraction call, pass `think=False`:

```python
vllm_client.complete(port, model, messages, max_tokens=10000, timeout=120.0, think=False)
```

`think=False` was added on 2026-07-17 (`6f15f87`) — it injects the vLLM-native
`chat_template_kwargs={"enable_thinking": False}`, and only on the opt-out path, so the orchestrator's
own reasoning calls are unchanged. **Grep check:** as of today no tool other than the HPO mapper passes
`max_tokens` to `complete()`. If PaperQA2 talks to the local endpoint through its own client rather than
`vllm_client`, apply the equivalent setting there (`paperqa_search._build_settings` /
`_local_endpoint` is where the endpoint is assembled) — the failure mode is a property of the *model*,
not of our wrapper.

Full write-up: [`phenotype-pipeline-validation.md`](phenotype-pipeline-validation.md) §6.

## 2. There is no plain-chat path today — one is being built

Every message currently goes to `POST /api/lab` and runs the **full research lab** (PI agenda →
multi-step execution → assembled report). A one-line question triggers the whole pipeline.

Note the existing `mode` field (`"single" | "team" | "auto"`) is **not** chat-vs-research — it selects
one scientist vs a Virtual-Lab team.

**In flight (started 2026-07-17, separate branch):** a lightweight ReAct-style fast path — first sentence
streams immediately, reasoning/tool-use happens in later loop turns — that can still call tools.
**That is where paper Q&A should land**: a literature question does not need an agenda and a report
bundle. Coordinate with Yijun on the branch before wiring your I/O into `/api/lab` directly.

## 3. What the chat can render for you (relevant to answer formatting)

The console's `renderMarkdown()` (`frontend/console/app.js`) is a small hand-rolled pipeline, **not** a
full Markdown engine. Currently supported: fenced/inline code, `#`/`##`/`###` headings, `-`/`*` bullet
lists, **bold**, _italic_, and — added 2026-07-17 — **GFM pipe tables**.

- **Tables now render.** Before that date a `| col | col |` answer displayed as a wall of raw pipes.
- **Mermaid is being added** in the same in-flight branch (client-side; `mmdc` is *not* installed on the
  prod server, only graphviz `dot`), so the model will be able to emit a ` ```mermaid ` block that draws
  inline. Useful if you want an answer to show a small evidence/derivation diagram.
- Anything else (nested lists, blockquotes, footnotes, links) is **not** rendered — plan citation
  formatting around what exists, or ask for the renderer to be extended.

**Citations:** the report line already refuses to "silently rescue" missing literature (your 2026-06-30
references rewrite). Keep that property in the Q&A path too — an answer with no retrieved evidence must
say so rather than fall back to the model's own recall.

## 4. Repo conventions that will bite if missed

- **`SKILL.md` is the only file the agent loads.** `preset_pipelines/*/PROTOCOL.md` and
  `skills/*/SKILL.md` — the code globs `*/SKILL.md`; `grep -rn PROTOCOL src/` returns **zero**.
  `PROTOCOL.md` is a human-facing rendering, invisible to the model. Don't put behaviour in it.
- **Tool registration:** `src/bioagent/agents/registry.py`. A tool that is not attached there is
  `unknown tool` at run time even if it imports fine.
- **Reports:** dated write-ups go in `reports/<YYYY-MM-DD>/<slug>.md` — see
  [`reports/README.md`](../README.md). House rules that matter for the evaluation you are planning:
  cite the run behind every number, say **who** produced a result (a model-authored baseline is not a
  curated one), and state what a result does **not** support.
- **Evidence must not live only in a gitignored dir.** `sample_data/` and `experiments/*/results/` are
  ignored, and worktrees get deleted. Today two experiments' raw results were nearly lost that way.

## 5. Building the eval set (the "questions only answerable by reading the paper" plan)

One lesson from the phenotype line, offered because it applies directly:

When we compared "hand-picked" HPO terms against the model's, the baseline turned out to have been
**authored by an LLM**, not curated by a human — so what looked like *expert vs model* was really
*model vs model*, and it changed what the whole comparison meant. **Write down who authored each gold
answer** in your eval set. If a question's reference answer came from a model, it measures agreement,
not correctness.

Also worth knowing: our deterministic rubric on the protocol A/B **saturated** — both arms scored 11/11,
which licensed only "this rubric cannot tell them apart", not "they are equivalent"
([`protocol-vs-skill-format-ab.md`](protocol-vs-skill-format-ab.md)). Build headroom into your scoring so
a good result and a great one are distinguishable.

## 6. Open questions for you

1. Does PaperQA2 reach the served Qwen through `vllm_client`, or its own OpenAI-compatible client? That
   determines whether §1's `think=False` applies directly or needs an equivalent on your side.
2. What I/O shape do you want for a paper question — answer + citation list + optional diagram? Whoever
   builds the fast path needs that contract.
3. Where do your HPC3 embeddings live, and do they need to be reachable from the **gateway process**
   (eyeserver, in-process) or from a **Slurm job** (HPC3)? That decides whether it is an in-process tool
   like `map_phenotype_to_hpo` or an offloaded one like the VEP/LIRICAL line — and the bind-mount rules
   differ sharply (a gateway-local path is *not* visible to an HPC3 compute node; that bug cost us a
   day this month too).
