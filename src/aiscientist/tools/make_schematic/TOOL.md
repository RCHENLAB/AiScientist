---
name: make_schematic
summary: Renders a workflow, pathway or mechanism diagram from text (Graphviz, D2 or Mermaid) into the run's figures.
category: figure
runs_on: inprocess
entry: tool:make_schematic_tool
order: 600
line: figure
owner: reporting
---
# make_schematic

Turns a diagram written as text into a figure, with a deterministic renderer: Graphviz `dot` (PNG, best in the PDF; the default), D2 (SVG) or Mermaid. No model draws pixels.

## When the agent uses it

- For conceptual figures in a report: the analysis workflow, a signalling pathway, a proposed mechanism.
- The gateway also uses its DOT renderer for the report's workflow figure (`render_dot`, `workflow_schematic_dot` through `tools/api.py`).

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `source` | string | required | diagram text in the chosen engine's syntax |
| `engine` | string (`dot`, `d2`, `mermaid`) | — |  |
| `basename` | string | — | short name for the figure file |
<!-- /generated:parameters -->

## Outputs

- A figure under `artifacts/figures/` and its path.

## Where it runs

<!-- generated:runs-on -->
In the gateway process; it returns in seconds and needs no HPC3 job.

Needs: `graphviz`.

Reads private data: no.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- Not for data plots; those come from the analysis tools.
- Graphviz must be installed on the gateway; D2 and Mermaid are optional.

## Code and tests

- `tool.py`: the renderers and the record.
- Tests: `tests/test_schematic.py`.

## What the model is told

<!-- generated:model-description -->
> Draw a SCHEMATIC diagram (analysis workflow, signaling pathway, or mechanism) by writing its
> structure as text — a deterministic renderer turns it into a figure in the gallery (NO AI draws
> pixels). Engines: `dot` (Graphviz, PNG, best for the PDF; default), `d2` (designer-grade SVG,
> best for web preview), `mermaid` (SVG/PNG). Use for conceptual figures, not data plots. Example
> DOT: 'digraph { QC -> Clustering -> "DE per cluster" -> Enrichment }'.
<!-- /generated:model-description -->
