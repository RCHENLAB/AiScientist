# Variant-annotation APIs (reference)

The `annotate_variants` tool (`src/bioagent/tools/variant_annotation.py`) uses these public APIs — no
key required. API notes adapted from the **k-dense-ai/scientific-agent-skills** references
(`database-lookup/references/{ensembl,clinvar}.md`, `tiledbvcf`).

## Ensembl VEP REST (functional consequence)

```
POST https://rest.ensembl.org/vep/{species}/region        # GRCh38 (default)
POST https://grch37.rest.ensembl.org/vep/{species}/region  # GRCh37 / hg19
Headers: Content-Type: application/json, Accept: application/json
Body:    {"variants": ["CHROM POS ID REF ALT . . .", ...]}   # VCF-style, <= 200 variants/request
```

Response (per variant, aligned to input order via the `input` field):
- `most_severe_consequence` — e.g. `missense_variant`, `stop_gained`, `frameshift_variant`.
- `transcript_consequences[]` — `{gene_symbol, gene_id, consequence_terms[], impact (HIGH/MODERATE/
  LOW/MODIFIER), amino_acids, polyphen_prediction, ...}`.
- `colocated_variants[]` — `{id ("rs..."), clin_sig[] (ClinVar significance), frequencies, ...}`.

## ClinVar (clinical significance / pathogenicity)

ClinVar significance is taken from VEP's `colocated_variants[].clin_sig` (one round-trip). For deeper
ClinVar detail (condition, review status) the NCBI E-utilities are available as a follow-up:
```
GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=clinvar&term={q}&retmode=json
GET https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=clinvar&id={ids}&retmode=json
```
Rate limit: 3 req/s (no key) or 10 req/s (with `&api_key=...`).

## TileDB-VCF (scalable variant database — optional, `scripts/build_variant_db_tiledbvcf.py`)

Sparse-array store for many single-sample **indexed** VCFs: incremental sample addition (no merges),
compressed storage, fast region/sample queries, local or cloud (s3/az/gs) URIs. Heavy optional dep
(`tiledbvcf-py`), installed in the analysis image — not the gateway env.
