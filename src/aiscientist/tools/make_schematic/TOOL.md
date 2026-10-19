---
name: make_schematic
summary: Renders a workflow, pathway or mechanism diagram written in Graphviz DOT into the run's figures.
category: figure
runs_on: inprocess
entry: tool:make_schematic_tool
order: 600
line: figure
owner: reporting
---
# make_schematic

Turns a diagram written as Graphviz DOT text into a PNG figure with the deterministic `dot` renderer. No model draws pixels. Diagrams in the chat are not drawn here: the model writes a Mermaid fence there and the browser renders it.

## When the agent uses it

- For conceptual figures in a report: the analysis workflow, a signalling pathway, a proposed mechanism.
- The gateway also uses its DOT renderer for the report's workflow figure (`render_workflow_schematic` through `tools/api.py`): one box per plan step holding its number and title, a long plan folded into rows, and the layout chosen so the figure stays between 1:2 and 4:1.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `source` | string | required | the diagram in Graphviz DOT |
| `basename` | string | — | short name for the figure file |
<!-- /generated:parameters -->

## Outputs

- A figure under `artifacts/figures/` and its path.
- For a PNG: its `width` and `height` in pixels, and a warning when one side is more than 8 times the other. Such a figure would print as a strip, and the report does not embed it (`reporting/figure_checks.py`).

## Where it runs

<!-- generated:runs-on -->
In the gateway process; it returns in seconds and needs no HPC3 job.

Needs: `graphviz`.

Reads private data: no.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Not for data plots; those come from the analysis tools.
- Graphviz must be installed on the gateway. DOT is the only input: a call that names another engine is refused with that message.
- The workflow figure shows each step's title only (its bold opening, else its first clause, cut to three short lines). The full text of a step is in the plan and in the Methods.

## Code and tests

- `tool.py`: the renderers and the record.
- Tests: `tests/test_schematic.py`.

## What the model is told

<!-- generated:model-description -->
> Draw a SCHEMATIC diagram (analysis workflow, signaling pathway, or mechanism) by writing its
> structure as text — a deterministic renderer turns it into a PNG figure in the gallery (NO AI
> draws pixels). Write it in Graphviz DOT. Use for conceptual figures, not data plots. Example:
> 'digraph { QC -> Clustering -> "DE per cluster" -> Enrichment }'.
<!-- /generated:model-description -->
