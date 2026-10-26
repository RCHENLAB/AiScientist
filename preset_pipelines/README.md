# Pipelines

Each folder here is a pipeline: a protocol for one kind of study, which the PI plans from. A
pipeline steers the plan and does not replace it. The PI still drafts the steps for the actual
dataset and question, the user reviews and edits the plan, and the Scientist and the Critic run
and check every step.

How pipelines relate to tools and skills, and how a run gets its pipeline, is in
[`docs/skills_and_pipelines_architecture.md`](../docs/skills_and_pipelines_architecture.md).

| Pipeline | Data | For |
|---|---|---|
| `celltype_annotation` | single-cell | QC, clustering, cell-type labels and a report |
| `differential_expression` | single-cell | a condition against a control, per cell type, with pathways |
| `gene_signature_scoring` | single-cell | scoring cells for a gene signature across clusters and conditions |
| `perturbation_analysis` | single-cell | a pooled CRISPR or Perturb-seq screen |
| `scgpt_annotation` | single-cell | per-cell labels from scGPT, and a cross-check of existing labels |
| `variant_annotation` | VCF | annotating and prioritizing the variants of a VCF |
| `phenotype_variant_diagnosis` | VCF and clinical notes | a rare-disease case: variants and phenotype reconciled |

## Layout

```
preset_pipelines/<name>/
  SKILL.md       required: front matter and the guidance the PI plans from
  PROTOCOL.md    required: the same protocol written for a researcher to read and audit
  references/    optional: supporting notes
  examples/      optional: a small input to try it on
```

`SKILL.md`:

```markdown
---
name: <the folder name; also the key the console sends>
description: <one line: shown in the console, and what the router reads to pick a pipeline>
tools: <comma-separated tools this protocol composes>
data_type: scrna        # or: variants. Leave it out for a protocol that fits any data.
---

<The guidance: when this protocol applies, the ordered steps and the tool each one uses, and the
 rules the results must respect. Where no tool covers a step, name the skill to adapt.>
```

The platform loads `SKILL.md` only (`src/aiscientist/agents/preset_pipelines.py`). `PROTOCOL.md` is
for people, so keep the two in step when one changes.

## Adding a pipeline

Add the folder and restart the gateway: pipelines are loaded when it starts. No code changes.
`AISCIENTIST_PIPELINES_DIR` points the loader at another directory.

`tests/test_pipeline_library.py` checks that every folder has both files, that `name` equals the
folder name, and that `tools` lists only tools that exist.

## Writing one

- **Compose tools, do not reimplement them.** A step that a tool covers names the tool. A step
  that no tool covers names a skill of the pinned [aiscientist-skills](https://github.com/lijinbio/aiscientist-skills) release, which the
  Scientist adapts and runs with `run_code`.
- **State what the data must have** (a condition column with two groups, a VCF with a known genome
  build), so the plan can say when the protocol does not apply.
- **A pipeline is text the planner reads.** It cannot change what the planner is shown about the
  dataset, and it cannot add a capability. Those are code: a new tool, or a change to the platform.
