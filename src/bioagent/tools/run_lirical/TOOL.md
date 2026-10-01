---
name: run_lirical
summary: Ranks candidate diseases by LIRICAL's post-test probability from HPO terms, genotype-aware when a VCF is loaded.
category: annotation
runs_on: hpc:phenotype
entry: tool:make_phenotype_differential_tool
order: 520
line: phenotype
owner: phenotype
---
# run_lirical

Runs LIRICAL, a likelihood-ratio model over HPO and OMIM, to rank candidate diseases by a calibrated post-test probability given the patient's HPO terms. With a VCF and the Exomiser data it scores genotype-aware; otherwise phenotype-only.

## When the agent uses it

- To answer how confident each candidate disease is when symptoms overlap (for example retinitis pigmentosa vs Leber congenital amaurosis).
- Downstream of `map_phenotype_to_hpo`, and of `annotate_variants` when there is a VCF.

## Inputs

<!-- generated:parameters -->
| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `hpo_terms` | array of string | — | observed HPO term IDs for the patient's phenotype (required), e.g. ['HP:0000510','HP:0000662'] |
| `excluded_hpo` | array of string | — | HPO term IDs the patient explicitly does NOT have (optional) |
| `sample_id` | string | — | the proband's sample id in the VCF (optional) |
| `vcf_path` | string | — | VCF for genotype-aware scoring (defaults to the run's dataset) |
<!-- /generated:parameters -->

## Outputs

- The ranked differential: disease, gene, post-test probability, likelihood ratios.
- Every HPO ID is checked against the release first; unknown IDs are dropped and listed.

## Where it runs

<!-- generated:runs-on -->
As a Slurm job inside `lirical.sif` on HPC3, through `python -m bioagent.tools.phenotype_cli`. Without HPC3 it returns `not_installed` rather than a guess.

Reads private data: yes; its results stay inside the run.

On the fast chat path: no (research runs only).
<!-- /generated:runs-on -->

## Known limits

- It runs only on HPC3 (`lirical.sif` and its data); without HPC3 it returns `not_installed`.
- A gene that is not curated into OMIM/HPOA cannot rank at all; `diagnose_disease` adds the literature for those.

## Code and tests

- `tool.py`: the phenopacket, the LIRICAL command, output parsing, the in-process runner and the record. The job entry point is `../phenotype_cli.py`.
- Tests: `tests/test_phenotype_dx.py`, `tests/test_hpo_mapper.py`.

## What the model is told

<!-- generated:model-description -->
> Phenotype-driven DIFFERENTIAL DIAGNOSIS: rank candidate DISEASES by a calibrated post-test
> probability, given the patient's phenotype (as HPO terms) and — when a VCF is loaded — the
> variant findings. Runs LIRICAL (HPO/OMIM likelihood-ratio model) on HPC3, DOWNSTREAM of
> annotate_variants. Use it to answer 'how confident is each candidate disease' (e.g. 'RP 70% /
> LCA 20%') when overlapping symptoms can't pin the diagnosis alone. Get `hpo_terms` and
> `excluded_hpo` from map_phenotype_to_hpo — run it on the patient's clinical description FIRST
> and pass its output through. Do NOT write HPO IDs from memory: IDs one digit apart are different
> real phenotypes, and every ID here is checked against the HPO release (unknown ones are
> dropped). If a VCF + the Exomiser database are available it scores GENOTYPE-AWARE (variants
> sharpen the ranking); otherwise PHENOTYPE-ONLY (a valid posterior from symptoms alone). Returns
> a ranked list of diseases (name, OMIM/ORPHA id, gene, post-test probability, composite
> likelihood ratio). Reports ONLY LIRICAL's output; never invents a disease or probability.
> Returns status 'not_installed' if LIRICAL is not staged — then continue without the
> differential.
<!-- /generated:model-description -->
