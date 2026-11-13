# Issue 94 validation record

The migration covers 23 scientific tools in seven MCP packages. Validation uses
the released digest-pinned images, original test inputs, HPC3 Slurm allocations,
and an isolated Eye Server website. Production and staging deployments were not
changed. The website uses its selected user-provided Claude model; no substitute
model or exported provider credential is used.

The package versions, image digests, reviewed schemas and resources are in
[`mcp_servers.json`](../../src/aiscientist/agents/mcp_catalog/mcp_servers.json).
The matching skills release is `liujs123456/aiscientist-skills` `v0.2.5`, bundle
SHA-256 `afaac0c9b77a421e075ecc52c610a53563cd2f2dcd9d9d75e42889c4255cd1e6`.
All 39 native capabilities (including Atlas and status tools) register from that
real release and the packaged catalog without starting a job or contacting a server.

## Scientific execution and artifact checks

| Domain | Tools | Website run | Observed checks |
| --- | ---: | --- | --- |
| scRNA analysis | 11 | `db89ba14f023` | 25 CSV tables and five complete AnnData checkpoints match the reference; all 11 raw result payloads agree after exact known path normalization. |
| Data utilities | 2 | `db89ba14f023` | Actual staged input identity and deterministic inspection metadata match; the supplied DOT produces the original Eye Server's byte-identical 795 × 59 PNG. |
| CellQC | 1 | `a1608e696d7b` | Original three-library scientific result passes strict parity. After fixing collection, a status-only recovery exports all 24 files byte-for-byte, both species checkpoints, saved datasets and a browser-downloaded full ZIP. |
| Variant annotation | 1 | `25e53774b36d` | Two-record GRCh38 offline VEP fixture; seven scientific artifacts are byte-identical to the original implementation. |
| scGPT annotation | 1 | `0fb4aa8eb425` | All 480 labels and confidence values match the original A30 execution exactly. |
| Literature | 4 | `30e3f3c21630` | Public search identifiers, three corpus passages, a source-supported answer, and bounded extraction with zero unsupported gene promotions. |
| Phenotype diagnosis | 3 | `0532008b38de` | Present and absent HPO terms preserved; all 8,621 LIRICAL numeric fields, identifiers and ordering match the original parser and adjudicator. The cross-server literature result conservatively reports no supported PRPF31 association. |

The combined scRNA/data comparison passed 47 checks with unchanged tolerances
`rtol=1e-6`, `atol=1e-8`. AnnData comparison includes `X`, `obs`, `var`, `layers`,
`raw`, embeddings and graphs, not just dimensions or summary statistics. The
uploaded input SHA-256 remains
`914a6843c5637a712d75abbfaa087b63bd5fc1390aa4be4deaf4529204c04d1f`.

The full CellQC browser ZIP is 200,954,579 bytes, SHA-256
`da5df87aa2b0aa7f98a971d928506ba2598e9cbf45707df4be9b6c9823bada4c`.
Its 24 entries individually match the completed native task's files and sizes.
The recovery did not rerun QC. The initial cross-filesystem collector failure and
cancelled retry are retained in the validation evidence.

The literature extraction pilot read four passages and did not constitute an
exhaustive corpus screen. Five measured calls cost $0.001761 under the unchanged
$0.10 cap. The generated deletion/segregation answer is checked against its
retrieved source, PMID 12923864, DOI 10.1002/ajmg.a.20224; generated prose is not
required to be byte-identical across model calls. Zero extracted genes is the
observed result, not a failed attempt to manufacture a positive list.

## Findings requiring website follow-up

Scientific execution alone does not close the migration gate. The initial
website reports exposed loss of substantive evidence when a response exceeded
the transport bound, a canonical-name mismatch when reusing native citations,
and an overbroad path filter that changed two disease labels containing slashes.
The original downloads and failures are retained. Corrected task-specific status
collection and grounded report regeneration must be verified before this record
can describe the complete website acceptance as passed.

Report review also caught ORA's top-100 gene restriction being incorrectly
applied to GSEA, whose actual ranking contains 1,502 genes for each group.
CellQC's original planning text incorrectly said `ambient_compare` was undeclared;
the released schema declares it. Historical planning text is evidence of that
error, not authority for a scientific result. Report-only regeneration must reuse
the completed run's accepted evidence and preserve its original scientific plan.

## Reproducibility boundaries

- CellQC's R/Python dependency environment is frozen. Earlier strict comparisons
  exposed four DecontX diagnostic-column differences when BLAS thread counts
  differed. Controlled replay reproduced that dependence; same-environment,
  same-thread execution passes. Those failed comparisons were not removed or
  accepted by loosening the `1e-10` tolerance. No BAM was supplied, so nuclear
  fraction is outside this acceptance.
- scRNA `auto` integration retains the original ComBat behavior. Explicit Harmony
  is a separately tested capability; the original production image lacked that
  dependency. Native final-image Slurm job `59540145` and original-image
  `59540080` provide the scientific baseline checks.
- scGPT comparison uses matching GPU hardware. The independent V100 native check
  (`59536819`) and the website A30 check are separate evidence; cross-hardware
  confidence variation is not hidden by changing tolerances.
- The variant fixture does not cover REST annotation, optional predictors or
  patient VCFs. The phenotype input is synthetic. Its ranked differential is not
  a clinical diagnosis, and unsupported literature evidence is not promoted.
- The schematic preserves the supplied DOT and original Graphviz behavior,
  including its thin aspect ratio; validation does not silently redesign it.

Full local evidence, native task outputs, comparison scripts, screenshots and
browser downloads are kept under `issue94-evidence/` in the development workspace.
Large inputs, credentials and scientific matrices are not committed to this repo.
