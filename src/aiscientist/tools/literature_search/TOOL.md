---
name: literature_search
summary: Finds real papers in Europe PMC for keyword queries and returns ready-to-cite references.
category: literature
runs_on: inprocess
entry: tool:make_literature_search_tool
chat: true
order: 400
line: literature
owner: literature
---
# literature_search

Searches Europe PMC (free, no key) with keyword queries built from a run's findings and returns real papers with title, authors, year, journal, DOI/PMID and a citation string. The report may cite only what a literature tool returned.

## When the agent uses it

- Before writing interpretation or discussion, with queries built from accepted findings (genes, pathways, disease, cell type).
- On the fast chat path, to answer a question with references.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `query` | string | required | keyword query (genes/pathways/disease/cell type) |
| `limit` | integer | — | max papers to return (1-25, default 8) |
<!-- /generated:parameters -->

## Outputs

- A list of papers, each with a ready-to-use `citation`. No files.

## Where it runs

<!-- generated:runs-on -->
In the gateway process; it returns in seconds and needs no HPC3 job.

Reads private data: no.

On the fast chat path: yes.
<!-- /generated:runs-on -->

## Known limits

- It finds papers; it does not read them. For an answer with evidence, use `deep_literature`.
- It calls the Europe PMC API from the gateway, so it needs the gateway's network.

## Code and tests

- `tool.py`: the query focusing (`focus_literature_query`, also used by the planner through `tools/api.py`), the API client and the record.
- Tests: `tests/test_literature_search.py`.

## What the model is told

<!-- generated:model-description -->
> Search published biomedical literature (Europe PMC) for REAL citations to ground the report.
> Pass keyword queries built from your findings — genes, pathways, disease, cell type (e.g. 'DDX41
> photoreceptor degeneration retina'). Returns real papers with title, authors, year, journal,
> DOI/PMID and a ready-to-use `citation` string. Cite ONLY papers this returns — never invent a
> reference. Use it before writing interpretation/discussion.
<!-- /generated:model-description -->
