# Issue 94 validation record

**Scientific execution and source-to-browser artifact fidelity pass for all 23 migrated tools. All four targeted main/technical report gates also pass with recorded limitations. Source retirement is authorized and being integrated; final combined-source CI and post-retirement service checks remain pending.** The remaining gates below are not implied by the scientific results.

The seven migrated packages ran through the actual isolated Eye Server website and HPC3 Slurm service, using released digest-pinned images. Production and the existing staging deployment were not overwritten. The website's selected Claude Haiku session was observed in the UI; downloaded scientific bundles do not independently attest the provider's model identity. Operator model tests, website model calls, scientific computation and report generation are distinct evidence.

Immutable package/image identities, tools/list hashes and deployment resources are recorded in the packaged [`mcp_servers.json`](../../src/aiscientist/agents/mcp_catalog/mcp_servers.json). The matching installed skills are `liujs123456/aiscientist-skills` `v0.2.5`, bundle SHA-256 `afaac0c9b77a421e075ecc52c610a53563cd2f2dcd9d9d75e42889c4255cd1e6`. Actual loading of this release registered **39 native capabilities: 23 migrated scientific tools, seven job-status helpers and nine pre-existing Atlas tools**, with zero external calls. This registration check is not execution of 39 analyses, and a static declaration does not bypass session or model availability checks.

## Scope and actual scientific acceptance

Tool names below are native server names; this application's migrated tools use their registered `mcp_` planner names. Status helpers and Atlas are excluded from the 23-tool migration count.

| Package / final version | Migrated scientific tools | Actual website run and result |
| --- | --- | --- |
| `cellqc` 0.0.5 | `run_cellqc` (1) | `a1608e696d7b`: three libraries, nine strict scientific checks; the original completed task was recovered after an artifact-collection failure. All 24 delivered files and both species checkpoints were verified. |
| `scrna-analysis` 0.0.5 | `run_scanpy_qc`, `run_doublet_detection`, `run_integration`, `run_clustering`, `run_de`, `run_depth_matched_de`, `run_pseudobulk_de`, `run_composition`, `run_enrichment`, `run_gsea_prerank`, `run_marker_annotation` (11) | `db89ba14f023`: all 11 raw native results, 25 CSV tables and five AnnData checkpoints agree with the reference. Together with data utilities, 47 scientific/artifact checks pass. |
| `scgpt-annotation` 0.0.3 | `scgpt_annotate` (1) | `0fb4aa8eb425`, job `59540287`: all 480 labels and confidences equal the original A30 reference; maximum confidence difference is zero. |
| `variant-annotation` 0.0.2 | `annotate_variants` (1) | `25e53774b36d`, job `59540165`: two public GRCh38 example records, offline VEP112/ClinVar; seven scientific files match the original byte-for-byte. |
| `literature` 0.0.4 | `literature_search`, `search_corpus`, `deep_literature`, `extract_haploinsufficient_genes` (4) | `30e3f3c21630`: real public identifiers, three corpus passages, a source-supported answer and bounded extraction. Subsequent same-run status recovery preserves all four full results and all CSV bytes in the ordinary browser download. |
| `phenotype-diagnosis` 0.0.4 | `map_phenotype_to_hpo`, `run_lirical`, `diagnose_disease` (3) | `0532008b38de`: present/absent HPO mapping and synthetic diagnosis execute successfully. Corrected same-run status recovery preserves all 8,621 original parser/adjudicator rows, including labels, reconciled rows and all eight literature records, in the browser download. |
| `data-utilities` 0.0.2 | `inspect_dataset`, `make_schematic` (2) | `db89ba14f023`: staged-input identity and deterministic inspection metadata match; the exact supplied DOT produces the original Eye Server Graphviz 2.42.4 byte-identical 795 × 59 PNG. |

All 23 have actual website primary-call witnesses whose immutable pins match the final catalog: 22 initial clean calls and one original CellQC task recovered after collection failed. The cancelled CellQC retry, earlier image comparisons and unsuccessful reports remain preserved. A status recovery is not counted as a new scientific execution.

### Reference fidelity

- **scRNA:** final published-image job `59540145` checks all 11 tools against unchanged original functions in the frozen environment and the ten supported calls against actual old production-image outputs. Explicit Harmony is an added capability because the old image lacks that dependency. Omitted/`auto` integration preserves deployed ComBat. Numerical tolerances remain `rtol=1e-6`, `atol=1e-8`; labels and identifiers are exact. The website comparison includes `X`, `obs`, `var`, layers, `.raw`, embeddings and graphs. Arbitrary `uns`, plot bytes and generated prose are not certified as equal. Known artifact-root translation is the only result-path normalization. The 480-cell scRNA input SHA-256 is `914a6843c5637a712d75abbfaa087b63bd5fc1390aa4be4deaf4529204c04d1f`.
- **scGPT:** final-image V100 job `59536819` and website A30 execution each compare against an original reference on the same GPU model. The 480 × 36,601 query, school model and harness were real, with external assets mounted read-only. Cross-GPU confidence differences remain separate failed diagnostics; this proves bounded migration equivalence, not annotation accuracy.
- **CellQC:** the scientific closure is frozen. Earlier strict comparisons exposed four DecontX diagnostic differences under different BLAS thread counts; original-versus-original controls reproduce that dependence, and controlled equivalent runs pass without relaxing `1e-10`. Only the 34 required source files and provenance were staged; no BAM-derived nuclear-fraction or whole-BAM processing claim is made.
- **Literature:** the corrected browser comparison is exact for four full JSON scientific payloads and three CSVs, with only documented artifact-location mapping. Nineteen original native files and the original four task directories are unchanged; four status calls created no new scientific tasks. This does not imply zero gateway reasoning/report calls. The four-passage extraction pilot returned zero eligible genes. Its $0.001635 pilot spend is included in the $0.001761 extraction total for five calls under the unchanged $0.10 cap; these are not the total four-tool workflow or report costs.
- **Phenotype:** the original slash-containing disease-label corruption is retained as a failed export. The corrected normal-browser ZIP now passes all 8,621 rows with no text normalization. The original HPO mapping step is unchanged. Mapping uses `HP:0000662`/`HP:0000580`; the separately fixed ranking input uses `HP:0000662`/`HP:0001133`. They are distinct fixtures, not an inferred automatic mapping-to-ranking handoff. PRPF31 remains `association=false`, tier `NONE`, `unsupported`; tied rankings are not a unique clinical diagnosis.

## Report acceptance, separate from computation

The current main and technical report files were read in full and compared byte-for-byte with normal browser downloads. Report-only revisions preserve the original scientific state, plan and accepted artifact hashes. Their targeted semantic gates do not certify every automatic audit, layout statement or biological inference.

| Run | Latest reviewed revision | Gate |
| --- | --- | --- |
| CellQC `a1608e696d7b` | `f698fa3d06d7`, evidence `20261113T120205Z` | **PASS_WITH_RECORDED_LIMITATIONS.** Thresholds, cell/doublet counts, ambient processing, both species, saved datasets and the complete delivery are described correctly; the historical `ambient_compare` schema error is explicitly corrected. |
| HECA `db89ba14f023` | `60213c4b8aa7`, evidence `20261113T121008Z` | **PASS_WITH_RECORDED_LIMITATIONS.** ORA's top-100 selection is distinct from GSEA's 1,502 genes per group; DE thresholds, the periphery/fovea contrast, sample/cell counts and clustering provenance are retained in both documents. |
| Literature `30e3f3c21630` | `0e86afd05ff2`, evidence `20261113T121108Z` | **PASS_WITH_RECORDED_LIMITATIONS.** Both reports contain the retrieved answer/source, eight deep contexts, three search passages, zero eligible extracted genes and the correctly scoped extraction cost. |
| Phenotype `0532008b38de` | `c96bb649529e`, evidence `20261113T121857Z` | **PASS_WITH_RECORDED_LIMITATIONS.** Fourteen checks pass: browser/gateway reports and all three accepted source artifacts match; negative PRPF31 evidence, 8,621-row ties, eight contexts, distinct HPO fixtures and original state/plan are retained. The earlier failed-before-writer duplicate-copy attempt remains archived. |

Known report limitations remain explicit:

- The generic numeric matcher is not a semantic verifier: it associated ARR3 detection of 2.3% with an unrelated depth counter, although the actual marker warning supports that percentage. Some values remain automatically untraced. Such matches are not promoted to scientific validation.
- CellQC's automatic QC accounting and literature's automatic citation counter do not recognize every status-recollection record. Their false-negative footer/counter statements do not invalidate the independently verified full task evidence. Historical critiques remain labelled historical. CellQC retention differs by 0.1 percentage point between direct division and rounding an already rounded native fraction; underlying cell counts agree.
- The literature report's phrase “screened” overstates the indexed corpus of 1,736 papers/426,706 sentences: only four passages were model-judged. Both reports retain that bound. The central segregation result is one primary family report (PMID 12923864; DOI 10.1002/ajmg.a.20224); fidelity to the retrieved protein-notation text is not independent clinical validation. Zero extracted genes is bounded abstention, not biological absence.
- Phenotype reports preserve the independently fixed mapping and ranking HPO sets; suggestions to reconcile them are future analysis, not permission to rewrite accepted inputs. The technical TSV locator is imprecise (the actual TSVs live under the collected `phenotype/` directory, not the task JSON directories). Task-specific JSON paths and all source bytes are correct. Revision instructions alone are not scientific evidence, and an automatic 15/15 numeric match is not semantic proof.
- Reports do not independently revalidate temporary work checkpoints or newly publish datasets. Existing delivered-file, saved-dataset and ZIP evidence is separate. Report-job render-review warnings remain recorded; semantic review does not certify every automated layout-pass sentence.
- The HECA study is a 480-cell regression subset, with 20 GSEA permutations, cell-level pseudoreplication, three samples per tissue arm and a fixed marker panel. The requested Harmony result is separate from this run's clustering: clustering reads the QC checkpoint and recomputes PCA. Existing `majorclass` labels are retained downstream. The thin 795 × 59 schematic is a fidelity check with a page-fit warning, not publication-layout acceptance.

Standalone model-backed services require the configured supported model transport; these tests do not establish direct native Anthropic support or authorize a substitute model. The website's existing selected-session model/budget path is separate. Variant validation excludes REST mode, optional predictor plugins, other builds and patient VCFs. These are bounded software/algorithm equivalence checks, not clinical or discovery claims.

## Retirement and deployment checks

All scientific, browser-fidelity and targeted report gates passed before the remaining legacy implementations were removed. The application now removes the 23 old scientific factories/implementations and dedicated dispatch/image definitions while retaining general CodeAct, bounded metadata preflight, dependency handling, result export/adoption and reference/deployment assets that those platform paths still require. Native registered schemas replace legacy tool aliases; unknown `mcp_` names acquire no capabilities.

The isolated combined retirement candidate based on App `5c44f74` passes **5,280 tests, 68 skips, no deselections**. AST/file checks preserve the current durable status recovery, report and delivery-context functions and find no imports of the 23 retired tool modules or `_lib.scrna` in the candidate source/tests. Scientific tests formerly hidden behind optional app imports were moved to MCP: native DE consumption of a processed checkpoint, refusal of repeat QC, declared defaults and the real published Atlas skill's table-consumption contract.

MCP test-only commit `96de689f82d8b5d046fc21c83bda9b538024c300` passes CI `43420259153`: Python 3.12 and 3.13 each **848 passed / 24 skipped**, nine container builds, lint/dependency audit, index and pip installation. No runtime/image tag changed for those tests. The optional Atlas consumer also passed locally against the actual skills 0.2.5 script, SHA-256 `ff6109cb3344df4c53e18ae762770e7d8cb1e08454c9ac4aa2095612f912363b`.

The retained platform worker already passed 12 real Slurm checks in job `59542533`: metadata preflight and scientific-dispatch refusal through both CLI entry points, original input preservation, real AnnData export/adoption with matrices/labels/provenance, rejection of an undeclared package without installation, and a real generic CodeAct subprocess. This is the pre-retirement control. Its final replay is recorded separately against the exact application source archive and allocation.

The final deployment record and PR validation identify the exact source commit, CI run, source/archive hashes, retained platform replay and browser smoke result. Their acceptance criteria are:

1. The integrated application preserves the subsequent status/report fixes, passes its full checks and exact-commit CI, and is deployed only to the isolated test site.
2. The same 12-check platform control runs against that application source artifact on Slurm and records source/SIF hashes and allocation identity. The installer check covers refusal of an undeclared package, not a fresh successful package installation.
3. The real System/current-session inventory contains 39 native declarations with honest availability and zero executable legacy 23 names. A two-step website `mcp_inspect_dataset`/`mcp_make_schematic` smoke uses the selected public HECA file; returned task identities and downloaded artifacts are checked.

A draft script, a local stub, a pre-retirement run or an older deployment does not satisfy these final checks. The workspace records their observed outcome under `web-e2e/post-retirement/` and the final platform replay directory; see the PR for the corresponding committed application identity.

## Evidence index

The development workspace keeps `issue94-evidence/` outside this repository. Package/source identities and exact hashes accompany the following authoritative records; older acceptance files that still record a failed first export are historical, not the latest gate:

- `cross-domain-acceptance.{json,md}`: 23 primary call witnesses, final pin/schema membership and explicit report/deployment gates.
- `native-image-acceptance/FINAL_NATIVE_ACCEPTANCE.json`, `native-images/v004/manifest.json`, `final-image-acceptance/`: final-image native tests and original-environment controls.
- `web-e2e/scrna-db89ba14f023/website-scrna-parity.json`; `web-e2e/scgpt-0fb4aa8eb425/website-parity.json`; `web-e2e/variant-25e53774b36d/website-parity.json`.
- `web-e2e/cellqc-a1608e696d7b/website-download-parity.json`: dedicated 24-file ZIP, 200,954,579 bytes, SHA-256 `da5df87aa2b0aa7f98a971d928506ba2598e9cbf45707df4be9b6c9823bada4c`.
- `web-e2e/phenotype-0532008b38de/post-status-browser-bundle/strict-comparison.json` and `status-recollection/20261113T115100Z/browser-snapshot-parity.json`: corrected exact browser evidence.
- `web-e2e/literature-recollection/{acceptance.json,browser-comparison.json}` and `after-status/20261113T115331Z/native-no-recomputation-audit.json`: corrected exact browser evidence and unchanged native tasks.
- The four successful report `semantic-review.json` records at the timestamps in the report table; the earlier phenotype failed-before-writer record remains preserved.
- `retirement/final-5c44f74/{acceptance.json,preservation-check.json,full-suite.log}`, `retirement/mcp-ci-43420259153.json`, `retirement/hidden-contract-migration.json` and `retirement/slurm-platform/{acceptance.json,execution.json}`.

Credentials, private scientific inputs and large matrices are not committed here. Every acceptance update requires its observed record and exact identity; it must not overwrite earlier failure evidence.
