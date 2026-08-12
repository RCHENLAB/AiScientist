# Reports — 过程报告 / process reports

This folder holds **process reports**: dated write-ups of what was built or tested, what the numbers
actually were, and what they do and do not support. They are meant to be **read by a person** — an
advisor check-in, a validation summary, an experiment post-mortem — so they are plain Markdown with
tables, not raw logs or JSON dumps.

**Layout:** `reports/<YYYY-MM-DD>/<slug>.md`, dated by when the work was reported.

## Index

| Date | Report | What it covers |
|---|---|---|
| 2026-07-24 | [file-ingest-agent.md](2026-07-24/file-ingest-agent.md) | Feature ① — a general LLM-driven "skim any uploaded file and get the gist" step (`tools/dataset_inspect.py`): deterministic `peek_dataset` (VCF/HDF5/tabular/gz/unknown, never raises) + LLM `describe_dataset` grounded against the peek. The upload-time / no-GPU split (peek at upload, describe only when a model is up, never provisions), what's wired vs deferred, and open questions for Yijun. |
| 2026-07-20 | [fast-chat-path-and-inline-mermaid.md](2026-07-20/fast-chat-path-and-inline-mermaid.md) | Design + verification for the answer-first ReAct chat route (explicit toggle, not a classifier; reuses the existing `chat_token` protocol) and inline Mermaid in the console (client-side, lazily loaded, four sandbox layers — including the measured finding that `securityLevel:"strict"` alone still lets an `<img>` through). Lists what was never run against a live model. |
| 2026-07-17 | [phenotype-pipeline-validation.md](2026-07-17/phenotype-pipeline-validation.md) | Validation of the free text → HPO → LIRICAL line: the discrimination test (one identical VCF, two clinical notes, the note flips the top gene), what the pertinent negatives are worth, how the score is actually computed (no weights — likelihood ratios), and why the posterior must never be reported as confidence. |
| 2026-07-17 | [handoff-to-literature-line.md](2026-07-17/handoff-to-literature-line.md) | Handoff from the core/orchestrator line to the literature line (Ziyao): the empty-completion trap on the served reasoning model, the fast-path work in flight, what the chat can actually render, and repo conventions that bite. |
| 2026-07-17 | [protocol-vs-skill-format-ab.md](2026-07-17/protocol-vs-skill-format-ab.md) | A/B of the PROTOCOL format against SKILL.md as the agent-facing file: no measurable gain, ~40 % more tokens, worse judge faithfulness — and a rubric that saturated, so it could not really tell them apart. |

## House rules

- **Keep the artifacts.** Run outputs that back a claim — LIRICAL HTML (the per-term likelihood-ratio
  breakdown), judge JSON, logs — **must be retained from now on**. Several reports here are thinner than
  they should be because the underlying artifacts were discarded: the LIRICAL runs kept only the TSV, so
  "which HPO term was worth how much" cannot be answered without re-running.
- **Never let evidence live only in a gitignored directory.** `sample_data/` and
  `experiments/*/results/` are ignored, and worktrees get deleted. Anything worth citing gets rescued
  into a report here.
- **Every number cites its run** — HPC3 job id, date, or the script that produced it. No estimates
  presented as measurements; mark back-computed values as back-computed.
- **Say who produced a result.** If HPO terms, labels or predictions were authored by a model rather
  than curated by a clinician, say so — it changes what the comparison means.
- **Keep the wrong predictions in.** A prediction quietly rewritten after the fact is worthless. The
  phenotype report keeps one that was wrong, and explains why the tool was right.
- **State what the result does NOT support.** A saturated rubric, an N of 3, or an LLM standing in for a
  human reviewer all belong in a Limitations section, not in the summary line.

## What belongs here vs elsewhere

| Where | What |
|---|---|
| **`reports/`** (here) | Dated process reports for a human reader — evidence tables, measured numbers, verdicts, limitations. |
| `docs/` | Reference documentation and per-case deep dives, kept current and cited *by* reports. |
| `preset_pipelines/<name>/SKILL.md` | The pipeline definition the **agent** loads — the only one the code reads. |
| `preset_pipelines/<name>/PROTOCOL.md` | The researcher-auditable rendering of that SKILL.md — for people, invisible to the model. |
| `handoff/<line>/` | Running per-work-line handoffs (newest section on top), not point-in-time reports. |
