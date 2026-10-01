---
name: inspect_dataset
summary: Identifies what an uploaded file is (format, genome build, samples, matrix layout) from a bounded read of its head.
category: qc
runs_on: inprocess
entry: tool:make_inspect_dataset_tool
order: 100
line: data
owner: analysis
---
# inspect_dataset

Reads the first part of any uploaded file and reports what it is: the format, and for a VCF its genome build and sample ids, for an `.h5ad` or HDF5 file its group tree, for a table its columns. When the session serves a model, a short call turns that evidence into a one-line description with a confidence; without one, the deterministic evidence (`peek`) comes back alone.

## When the agent uses it

- At the start of a step whose bound dataset is of unclear type: an unusual extension, a renamed file, a folder upload.
- The gateway runs the same triage at upload and at run start (`describe_dataset` and `peek_dataset` through `tools/api.py`), so the planner routes by what a file contains rather than by its suffix.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `path` | string | — | server-side path of the file to skim. Omit to use the dataset already bound to this run. |
<!-- /generated:parameters -->

## Outputs

- Result keys: `peek` (format, header fields, sizes), `description` (`file_kind`, `likely_modality`, `one_line_summary`, `confidence`), and `raw_data_to_llm` (whether a head sample reached the model).
- No files are written.

## Where it runs

<!-- generated:runs-on -->
In the gateway process; it returns in seconds and needs no HPC3 job.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- It reads only a bounded head, so anything that appears only deep in a file is invisible to it.
- It runs in the gateway: a model-typed HPC3 path (`/dfs3b/...`) does not exist there, so the run's bound dataset wins over the `path` argument.
- Its LLM call goes only to the session's local model (`tools.sdk.session_chat_fn`). In a session that uses only an external API there is no description, just the peek.

## Code and tests

- `tool.py`: the reader for each format, the triage prompt and the record.
- Tests: `tests/test_dataset_inspect.py`, `tests/test_run_start_triage.py`.

## What the model is told

<!-- generated:model-description -->
> Skim an uploaded data file — ANY type, known extension or not — and return a structured 'what is
> this': format, genome assembly and sample id(s) for a VCF, the group tree for an .h5ad/HDF5
> matrix, columns for a table, or a best-effort descriptor for an unknown/binary file. Use this
> FIRST when you are unsure what a bound dataset actually is, instead of assuming from the
> filename. Reads only a bounded head; never loads the whole file. Returns `peek` (deterministic
> evidence) and `description` (file_kind, likely_modality, one_line_summary, confidence) — do not
> invent facts beyond what it reports.
<!-- /generated:model-description -->
