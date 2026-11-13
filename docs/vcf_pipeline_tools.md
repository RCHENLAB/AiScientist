# The variant line: native tools and reference data

Variant annotation uses the registered `mcp_annotate_variants` tool in the
independently released `variant-annotation` MCP image. Its reviewed schema, version
and immutable digest are in the [packaged catalog](../src/aiscientist/agents/mcp_catalog/mcp_servers.json).
The application stages the input, submits the native image on HPC3, collects
complete owned task artifacts and reports their provenance. It does not import
the retired annotation implementation or fall back to a gateway REST request.

The [native package documentation](https://github.com/liujs123456/aiscientist-mcp/tree/main/packages/variant-annotation)
defines all arguments, reference settings, scientific stages and result files.
Use the [reference staging guide](../deploy/vep/README.md) for deployment and
[HPC3 asset inventory](hpc3_assets.md) for the site's external data.

## Execution and inputs

- Offline mode is the default. The server runs bcftools filtering and optional
  normalization, VEP release 112 against a matching cache, configured optional
  annotations, reduction and result-table generation in its own image.
- REST mode requires both an explicit `mode="rest"` call and the operator's
  `VARIANT_ALLOW_REST=1`. It transmits loci and alleles to Ensembl, caps the input
  and reports truncation. Missing HPC access does not select REST automatically.
- The application preserves VCF-header assembly detection and configured fallback
  handling. Reference and predictor files must match the selected GRCh37/GRCh38
  build. An empty build-specific setting does not borrow another build's files.
- The deployment adapter retains configured gene-panel, region-BED and rarity
  defaults; explicit reviewed call arguments override them. The platform's IRD
  panel is [reference data](../src/aiscientist/agents/reference_data/ird_retnet.txt),
  not an annotation algorithm embedded in the gateway.

Reference caches, ClinVar, optional predictor files, FASTA, licensed databases and
SpliceAI assets remain operator-managed read-only mounts. The app's
`deploy/vep/build_and_stage.sh` stages references; it no longer builds `vep.sif`.
Image publication belongs to the MCP repository. The existing `AISCIENTIST_VEP_*`
reference settings are translated by the adapter; explicit native `VARIANT_*`
settings take precedence. Resource requests are reviewed catalog configuration.

## Results and other variant skills

`mcp_variant_job_status` retrieves an accepted native task by analysis and task ID.
Full JSON and seven scientific deliverables remain downloadable; bounded previews
are not the complete evidence. Optional normalization, predictors and annotation
layers are reported only when they actually ran. These annotations and ranking
heuristics are research outputs, not a clinical diagnosis.

The pinned skills release also provides independent CodeAct workflows such as
VCF normalization, callset QC, clinical-variant research prioritization, output
tables and TileDB-VCF. Their declared environments and reference requirements
still apply. They do not restore the retired application annotation factory.
The native variant and phenotype tools used by the
[preset pipelines](../preset_pipelines/README.md) retain their explicit review,
inputs and provenance.

## Migration validation

Website run `25e53774b36d` executed the final native image on HPC3 job `59540165`.
The two-record public GRCh38 offline fixture produced seven scientific artifacts
byte-identical to the original implementation. Acceptance used VEP112/ClinVar
with optional predictors disabled; it does not validate REST, other assemblies,
optional predictor databases or patient VCFs. See the
[issue 94 validation record](development/issue94_validation.md) for the full scope.
