# Demo dataset — `demo_variants.vcf`

A tiny, self-contained **GRCh38** VCF (8 well-known variants) for demoing the
`variant_annotation` skill end-to-end: Ensembl VEP consequence + SIFT/PolyPhen + gnomAD/1000G
rarity + **ClinVar pathogenicity**, and the clinically-actionable `high_priority` shortlist.

Coordinates + reference alleles were pulled from **Ensembl GRCh38** (so VEP maps them cleanly), and
the whole file was validated against the **live Ensembl VEP REST** through this skill's
`annotate_variants` tool (see "Expected result" below).

## The 8 variants

| rsID | gene | GRCh38 | change | why it's here |
|---|---|---|---|---|
| rs334 | HBB | 11:5227002 | T>A | sickle-cell (HbS) — **pathogenic** |
| rs6025 | F5 | 1:169549811 | C>T | Factor V Leiden thrombophilia — **pathogenic** |
| rs28929474 | SERPINA1 | 14:94378610 | C>T | α1-antitrypsin PiZ — **pathogenic** |
| rs1800562 | HFE | 6:26092913 | G>A | hereditary hemochromatosis C282Y — **pathogenic** |
| rs5030858 | PAH | 12:102840493 | G>A | phenylketonuria — **pathogenic** (rarest here, max AF ≈ 0.2%) |
| rs1042522 | TP53 | 17:7676154 | G>C | Pro72Arg — ClinVar **pathogenic** BUT very common (**AF ≈ 0.75**): the "note the frequency, a common variant is unlikely causal" teaching case |
| rs1801133 | MTHFR | 1:11796321 | G>A | C677T — common (AF ≈ 0.49), `drug_response` → **excluded** from the shortlist |
| rs1801282 | PPARG | 3:12351626 | C>G | Pro12Ala — common (AF ≈ 0.17), `likely_benign` → **excluded** |

So the set exercises every branch: clean actionable pathogenics, a pathogenic-but-common variant
(frequency caveat), and correctly-excluded common/benign variants.

## How to use it

- **Console:** upload `demo_variants.vcf` via the Data menu (the uploader accepts `.vcf`/`.vcf.gz`),
  then ask e.g. *"annotate these variants and flag the clinically actionable ones."* The planner
  routes a VCF to the `variant_annotation` workflow (not the scRNA line).
- **Ad-hoc / tests:** pass the path directly to the tool — `annotate_variants(vcf_path=".../demo_variants.vcf", assembly="GRCh38")`.

Needs network (VEP/ClinVar are REST calls) → runs on the local sandbox, **not** the network-off HPC3
analysis container.

## Expected result (validated against live VEP, GRCh38)

- **8 variants**, all `missense_variant` (MODERATE impact).
- **6 pathogenic** (HBB, F5, SERPINA1, HFE, PAH, TP53), **2 excluded** (MTHFR `drug_response`,
  PPARG `likely_benign`).
- **`high_priority` = 6** — the pathogenics; the report should still flag that TP53 rs1042522 has
  ~0.75 population AF (common → unlikely causal despite the ClinVar label).

ClinVar classifications drift over time, so exact counts can shift slightly on re-run — the shape
(several pathogenics surfaced, common/benign excluded) is what the demo shows.
