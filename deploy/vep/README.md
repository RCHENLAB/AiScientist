# Native variant annotation references

Variant annotation runs through the registered `mcp_annotate_variants` tool in the immutable
`aiscientist-mcp` image. The app no longer builds `vep.sif`, copies scientific Python into it,
or falls back to an in-process Ensembl request. The server supports explicit REST mode only
when the administrator enables `VARIANT_ALLOW_REST=1`.

`build_and_stage.sh` now stages reference data only: release-112 VEP caches and matching
ClinVar files for GRCh37 and GRCh38. Run it as an HPC3 compute job; bulk transfer uses the
school's transfer service. It keeps already downloaded references:

```bash
sbatch -p standard -A ruic20_lab -c 4 --mem=16G -t 08:00:00 \
  --wrap "bash $PWD/deploy/vep/build_and_stage.sh"
```

Set `VARIANT_VEP_CACHE_DIR_GRCH37`, `VARIANT_VEP_CACHE_DIR_GRCH38`, and the corresponding
`VARIANT_VEP_CLINVAR_GRCH37` / `VARIANT_VEP_CLINVAR_GRCH38` to the staged data. Existing
`AISCIENTIST_VEP_*` reference settings remain supported through the app's deployment adapter;
explicit `VARIANT_*` values take precedence, including empty values. The VCF header still wins
over the configured assembly fallback. Offline annotation processes the complete bound VCF.

The lab's existing default gene panel, rarity filter and region BED remain supported through
`AISCIENTIST_DEFAULT_GENE_PANEL`, `AISCIENTIST_DEFAULT_MAX_POP_AF`, and
`AISCIENTIST_DEFAULT_REGIONS_BED`; per-call arguments override these defaults. Their native
configuration equivalents use the `VARIANT_DEFAULT_*` prefix. BED files come from the exact
administrator-configured reference or the run's bound inputs.

Optional predictor data and IRD layers are still staged separately by `stage_annotation_dbs.sh`
and the other reference scripts in this directory. Preserve assembly-specific files: an empty
GRCh37 predictor does not fall back to GRCh38 coordinates. SpliceAI uses the administrator's
configured environment and model directory, mounted read-only; a model cannot choose an executable.
Image provenance, resource limits and tool schemas are recorded in `agents/mcp_catalog` and each
run's MCP result metadata. Scientific tests live in `aiscientist-mcp/packages/variant-annotation`.

The retired path was gated by original-runtime parity and a real website run (`25e53774b36d`,
HPC3 job `59540165`): all seven downloaded scientific result files matched the old baseline
byte for byte. That test used the two-record public fixture, offline VEP112/ClinVar and disabled
optional predictors; it does not claim a new validation of those optional databases.
