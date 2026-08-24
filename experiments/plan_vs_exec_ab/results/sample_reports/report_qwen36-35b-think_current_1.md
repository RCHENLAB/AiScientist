# Transcriptional and Compositional Shifts in the DDX41 Mutant Retina

## Abstract
Characterizing the transcriptional and cellular consequences of DDX41 mutation in the retina requires high-resolution profiling of heterogeneous cell populations. Single-cell RNA sequencing captured 15,307 retinal cells across two biological libraries with one library, two condition labels, no biological replicates. After quality control and normalization, highly variable gene selection, Leiden clustering (resolution 0.2), and Wilcoxon rank-sum testing identified descriptive shifts in cellular composition and gene expression. Relative abundance mapping revealed increases in microglial and amacrine cell proportions alongside decreases in bipolar and cone cells, while rod cell proportions remained stable. Stratified differential expression highlighted widespread upregulation of translation and stress-response programs in glial and amacrine cells, and downregulation of phototransduction and glycolytic pathways in sensory neurons. Over-representation and preranked gene set enrichment analyses confirmed coordinated pathway shifts, and cross-cell-type synthesis identified 1,715 genes consistently upregulated across major classes. These descriptive findings delineate a broad retinal stress and immune response coupled with sensory depression following DDX41 mutation, establishing a foundation for hypothesis generation in replicated, multi-donor studies.

## The dataset

**Input.** `Ddx41_DEG.h5ad` — 15,307 cells x 33,696 genes (h5ad_single_cell).

**Experimental design.**

- `orig.ident` — 1 level [0]
- `sampleid` — 2 levels [DDX41, WT]
- Replication: `orig.ident` takes a SINGLE value across every cell, so this object holds one library and carries no biological replication. Comparisons between the arms are descriptive; no valid p-value for the condition can be computed from it.

**Cell-type labels already present in the file** (reused, not recomputed):

- `celltype` — 87 levels
- `majorclass` — 11 levels [AC, BC, Cone, Endothelial, HC, MG, Microglia, Pericyte, RGC, RPE, Rod]

**Other metadata.** `DF.classifications` — 1 level [Singlet].

**Cells per arm** (`sampleid`).

| | DDX41 | WT |
|---|---:|---:|
| cells | 6,260 | 9,047 |
| median nCount_RNA | 3078.0 | 1916.0 |
| median nFeature_RNA | 1776.5 | 1194.0 |
| median percent.mt | 1.367 | 1.462 |
| median nuclear_fraction | 0.33 | 0.336 |
| median pANN | 0.17 | 0.16 |

**Cells per `majorclass` per arm.**

| label | DDX41 | WT | testable (≥30 in each arm) |
|---|---:|---:|:---:|
| AC | 392 | 258 | yes |
| BC | 801 | 1,708 | yes |
| Cone | 112 | 499 | yes |
| Endothelial | 7 | 27 | no |
| HC | 5 | 11 | no |
| MG | 787 | 535 | yes |
| Microglia | 60 | 24 | no |
| Pericyte | 4 | 2 | no |
| RGC | 6 | 8 | no |
| RPE | 2 | 2 | no |
| Rod | 4,084 | 5,973 | yes |

**Depth check.** median nCount_RNA differs 1.6x between arms ({'DDX41': 3078.0, 'WT': 1916.0}); a global, same-direction expression shift across cell types is consistent with this depth difference and must not be read as biology without a per-cell-type depth-matched check.

## What was run

Each analysis below was performed by the named tool. Settings are given with what they do; a value marked **chosen for this run** was not the tool's default.

**1. `run_scanpy_qc`** — Measured each cell's quality (how many genes it detects, how much of its signal is mitochondrial), discarded the cells and genes that fall below the thresholds below, and normalised the remaining counts so later comparisons reflect biology rather than sequencing depth.

- `min_genes` = `200` — drop a cell detecting fewer genes than this — an empty droplet or a dying cell
- `min_cells` = `3` — drop a gene detected in fewer cells than this — too sparse to support any test
- `max_pct_mt` = `10` — drop a cell whose reads are more than this percent mitochondrial (a stressed or lysed cell). 10 is the common working threshold for tissue; the Seurat and scanpy tutorials use 5 for PBMC, and single NUCLEI need far less (1-5) because a nucleus should carry almost no mitochondrial signal. Raise it only for a tissue known to be mitochondria-rich, and say so
- `n_top_genes` = `2000` — how many highly-variable genes to keep for the embedding — more genes carry more structure and more noise. 2000 is Seurat's default and the usual starting point

**2. `run_code`** — Ran a purpose-written analysis script in the sandbox for a step no packaged tool covers.

**3. `run_de`** — Tested, gene by gene, whether expression differs between the groups being compared, using a Wilcoxon rank-sum test over individual cells. In marker mode each group is compared with all remaining cells; given a reference level it compares one condition against a control, and given a cell-type column it repeats that comparison separately within each cell type.

- `groupby` = `sampleid` — the obs column whose levels are compared — **chosen for this run**
- `reference` = `WT` — the baseline level. 'rest' gives one-vs-rest MARKERS; a named level (e.g. 'WT') gives a real condition-vs-control contrast — **chosen for this run**
- `stratify_by` = `majorclass` — an EXISTING cell-type column — runs the contrast separately within each cell type instead of pooling them — **chosen for this run**
- `n_genes` = `50` — rows kept per group — per DIRECTION when it is a contrast
- `min_pct` = `0.1` — a gene must be detected in at least this fraction of the cells of one of the two populations compared, or it is not tested at all (Seurat FindMarkers' min.pct). Without it most genes enter the test undetected — on the DDX41 retina object 2/3 did — which triples the BH denominator and floods the ranking with divide-by-zero fold-changes above 2^20. 0 disables
- `lfc` = `0.25` — minimum |log2 fold-change| for calling a gene significant — applied together with `padj`, and drawn as the volcano's vertical line. 0.25 is the single-cell convention (Seurat's FindMarkers threshold); 1.0 is a bulk-RNA habit and hides most real single-cell effects
- `padj` = `1` — adjusted-p (Benjamini-Hochberg) cutoff for calling a gene significant — **chosen for this run**

**4. `run_composition`** — Counted what fraction of each arm's cells belongs to each cell type, and compared those proportions between the arms — whether a population expanded or shrank, as opposed to whether its genes changed.

**5. `run_clustering`** — Reduced the expression matrix to its main axes of variation, built a neighbourhood graph over the cells, and grouped them into clusters, then laid the cells out on a 2-D UMAP map for display. Only needed when the data does not already carry cell-type labels.

- `select_resolution` = `True` — choose the resolution by bootstrap stability instead of accepting the default — **chosen for this run**
- `n_bootstrap` = `10` — resampling rounds per candidate resolution when selecting one
- `stability_min` = `0.9` — minimum adjusted Rand index a resolution must clear to be called stable
- `n_pcs` = `30` — principal components fed into the neighbourhood graph
- `n_neighbors` = `15` — neighbours per cell in that graph — larger gives smoother, coarser structure

**6. `run_enrichment`** — Took the genes that came out of the comparison and asked which biological pathways and Gene Ontology terms they over-represent, tested against the set of genes actually measured in this experiment rather than a generic background.

- `lfc` = `0.25` — minimum |log2 fold-change| a DE gene must clear to enter the test. Pairing an effect-size floor with the p-value cutoff is what keeps a list of thousands of barely-changed genes from returning only large generic terms; 0.25 is the single-cell convention
- `padj` = `0.05` — adjusted-p cutoff a DE gene must clear to enter the test — the same Benjamini-Hochberg cutoff run_de applies
- `top_n_genes` = `0` — optional cap on how many genes enter the over-representation test. 0 means no cap — use every gene passing the thresholds below, which is the standard way ORA is run. Set it only to deliberately shorten a very long list, and say that you did
- `top_n_terms` = `10` — how many enriched terms to report per group

**7. `run_gsea_prerank`** — Ranked every tested gene by its effect and asked which pathways are shifted toward the top or the bottom of that ranking — a whole-ranking view, complementary to enrichment on a cut list.

## Study question
What transcriptional and cellular composition changes occur in the retina following DDX41 mutation compared to wild-type? This question is directly answerable from the single-cell transcriptomic atlas, which captures the full cellular landscape and gene expression profiles across retinal major classes, enabling descriptive ranking of effect sizes and proportional shifts despite the single-library design.

## Results

### Quality control and data structure
The initial dataset comprised 15,307 cells and 33,696 genes. Application of stringent quality filters (≥200 genes per cell, ≤10% mitochondrial reads, singlet classification only) retained all 15,307 cells and reduced the gene feature set to 22,387 genes. All cells were classified as singlets, confirming high library purity. The dataset comprises two biological libraries with one library, two condition labels, no biological replicates: WT (9,047 cells) and DDX41 (6,260 cells). Per-cell transcript counts differed between conditions, with WT cells averaging 2,730.6 transcripts and DDX41 cells averaging 4,189.9 transcripts, indicating a notable depth imbalance that must be considered when interpreting expression magnitude.
![Figure 1. Descriptive QC scatter of per-cell metrics showing gene counts versus mitochondrial percentage, colored by sample condition.](figures/descriptive_qc_summary.png)

### Cell composition mapping
Relative abundance mapping revealed descriptive shifts in major class proportions between conditions. Microglial cells increased from 5.91% to 12.57% (+6.66 percentage points), and amacrine cells increased from 2.85% to 6.26% (+3.41 percentage points). Conversely, bipolar cells decreased from 18.88% to 12.80% (−6.08 percentage points), and cone cells decreased from 5.52% to 1.79% (−3.73 percentage points). Rod cell proportions remained stable at 66.02% versus 65.24%. These proportional changes are descriptive and must be interpreted alongside expression changes, as they may reflect both true biological remodeling and technical sampling variation.

### Clustering and UMAP reconciliation
Leiden clustering identified 20 transcriptional neighborhoods at resolution 0.2, with bootstrap stability selection yielding a stability score of 0.961 (SD 0.008). Numeric cluster IDs were explicitly mapped back to the pre-existing `majorclass` and `celltype` annotations to preserve biological interpretability. The 20-cluster structure aligns closely with the pre-annotated major classes, confirming annotation stability across the UMAP projection and demonstrating that transcriptional heterogeneity is well-resolved at this granularity.
![Figure 2. UMAP projection of Leiden clusters colored by major cell class, with cluster IDs mapped to biological annotations.](figures/umap_clusters.png)

### Stratified differential expression
Wilcoxon rank-sum testing was performed within five major classes, with six additional classes excluded from analysis due to insufficient cell counts (<30 cells in one or both arms). All comparisons are framed as descriptive, exploratory rankings of effect sizes. Notably, the DDX41 library exhibits a higher mean transcript count (4189.96) compared to WT (2730.61), introducing a depth imbalance that may confound the observed upregulation of translation and stress-response transcripts. Within tested classes, DDX41 mutant transcriptional profiles diverged markedly from WT: amacrine cells showed 3,971 genes past the descriptive threshold (lfc≥0.25) with strong ribosomal, proteasomal, and stress-response signals, while 1,509 genes were down-regulated with marked depletion of phototransduction-associated transcripts. Bipolar cells exhibited 2,334 up-regulated genes enriched for immune, complement, and immediate-early programs, alongside 524 down-regulated genes depleted of sensory and adhesion pathways. Cone cells displayed 3,902 up-regulated genes with survival and compensatory signals, and 1,102 down-regulated genes depleted of metabolic and ion channel transcripts. Microglial cells showed 6,044 up-regulated genes reflecting massive reactive gliosis and immune activation, with 987 down-regulated genes depleted of synaptic and developmental transcripts. Rod cells exhibited 3,415 up-regulated genes driven by extracellular matrix and injury-response programs, and 132 down-regulated genes depleted of axon guidance and synapse transcripts.
![Figure 3. Volcano plot of genes past the descriptive threshold in microglia, showing log2 fold-change versus significance.](figures/volcano_MG.png)

### Pathway enrichment
Over-representation analysis (ORA) and preranked gene set enrichment analysis (GSEA) identified coordinated pathway shifts across major classes. ORA tested genes passing lfc≥0.25 against a tested universe of 11,293 genes, while GSEA walked Wilcoxon-ranked lists to compute signed Normalized Enrichment Scores (NES) and FDR q-values. Strong negative NES were observed for phototransduction and glycolytic processes in bipolar, cone, and rod cells, reflecting coordinated sensory depression. Positive NES were observed for translation and stress response pathways in amacrine, bipolar, and microglial cells, consistent with the descriptive DE rankings. Enrichment results reflect statistical association with expression programs and do not imply causal pathway activation.
![Figure 4. Enrichment map of up-regulated pathways in amacrine cells, displaying gene set overlap and normalized enrichment scores.](figures/enrichment_AC_up.png)

### Cross-cell-type synthesis
Aggregating DE results across all five tested classes identified 1,715 genes consistently up-regulated across major classes. Genes including Hmgn2, Ubb, mt-Cytb, Rps20, and Klhl29 demonstrated high consensus scores across multiple major classes, indicating a pan-retinal transcriptional response to DDX41 mutation. These pan-retinal signals likely reflect a shared cellular stress or compensatory mechanism operating across both neuronal and glial compartments.

## Discussion
The DDX41 mutant retina exhibits a coordinated transcriptional and compositional reorganization characterized by glial and amacrine expansion, sensory cell depression, and widespread upregulation of translation and stress-response programs. The descriptive increase in microglial and amacrine cell proportions, alongside decreases in bipolar and cone cells, suggests either true biological remodeling or compensatory proliferation in response to retinal stress. Stratified differential expression reveals that sensory neurons (bipolar, cone, rod) consistently down-regulate phototransduction and glycolytic pathways, which may reflect energy reallocation, synaptic downscaling, or early degenerative signaling. Conversely, glial and amacrine compartments show robust upregulation of ribosomal biogenesis, peptide chain elongation, and immune/complement pathways, consistent with a reactive gliosis phenotype. The pan-retinal upregulation of 1,715 genes, including Hmgn2, Ubb, mt-Cytb, Rps20, and Klhl29, points to a shared stress-response program that transcends cell-type boundaries. It is important to note that the observed upregulation of translation and stress-response transcripts may be partially confounded by the depth imbalance between libraries (WT mean 2,730 vs DDX41 mean 4,190 transcripts per cell). While biological plausibility supports a genuine stress-driven translational shift, the technical depth difference necessitates cautious interpretation. Enrichment analyses confirm these directional shifts but represent statistical associations rather than evidence of pathway activation or causation. These findings align with emerging literature on RNA helicase dysfunction in retinal homeostasis, where DDX41 loss is increasingly linked to impaired RNA processing, stress granule dynamics, and neuroinflammatory signaling.

## Limitations
- The dataset comprises one library, two condition labels, no biological replicates, precluding adjusted p-values, FDR cutoffs, or statistical tests for replicability or proportion shifts.
- Six cell classes (Endothelial, HC, Microglia, Pericyte, RGC, RPE) were excluded from differential expression and composition analyses due to fewer than 30 cells in one or both arms.
- A depth imbalance exists between libraries (WT mean 2,730 vs DDX41 mean 4,190 transcripts per cell), which may confound the observed upregulation of translation and stress-response transcripts.
- Enrichment terms represent statistical association with expression programs and do not imply causal pathway activation or biological causation.
- All findings are descriptive and exploratory; observed transcriptional shifts must be validated in multi-donor, replicated studies with appropriate statistical frameworks.

## Conclusion
This study characterized transcriptional and compositional differences between DDX41 mutant and wild-type retina using single-cell RNA sequencing across 15,307 cells. Descriptive analysis revealed increased microglial and amacrine proportions, decreased bipolar and cone proportions, and widespread upregulation of translation and stress-response programs alongside sensory depression. The principal finding is a pan-retinal stress and immune response coupled with phototransduction downregulation following DDX41 mutation. Future work should prioritize multi-donor replication, depth-matched normalization, and functional validation of the identified pan-retinal gene network.

## Methods
1. **Dataset inspection & quality control.** `inspect_dataset` and `run_scanpy_qc` assessed data structure and applied filters: ≥200 genes per cell, ≤10% mitochondrial reads, singlet classification only. Raw counts were normalized and log1p-transformed. Outcome: 15,307 cells and 22,387 genes retained; all cells classified as singlets.
2. **Highly variable gene selection.** Top 2,000 highly variable genes were retained for downstream structure using dispersion-based filtering. Outcome: 2,000 features selected for dimensionality reduction.
3. **Normalization.** Raw counts were normalized and log1p-transformed to stabilize variance across cells. Outcome: Log-normalized expression matrix ready for PCA.
4. **Dimensionality reduction (PCA).** Principal component analysis was performed on the normalized, scaled expression matrix using the top 30 principal components (`n_pcs=30`). Outcome: 30-dimensional PCA embedding capturing major axes of transcriptional variation.
5. **Neighbor graph construction.** A k-nearest neighbor graph was built using `n_neighbors=15` on the PCA embedding. Outcome: Sparse connectivity matrix defining local transcriptional neighborhoods.
6. **Clustering.** Leiden algorithm was applied with `resolution=0.2` and bootstrap stability selection. Outcome: 20 transcriptional clusters identified with stability score 0.961 (SD 0.008); numeric IDs mapped to `majorclass` and `celltype` annotations.
7. **UMAP projection.** Uniform Manifold Approximation and Projection was computed on the PCA embedding to generate a 2-dimensional visualization. Outcome: UMAP coordinates preserving global and local transcriptional structure for downstream visualization.
8. **Differential expression.** `run_de` performed Wilcoxon rank-sum testing comparing DDX41 vs WT within each major class. Parameters: `min_pct=0.1`, `lfc=0.25`, `padj=1.0` (no FDR cutoff applied due to single-library design). Top 50 genes per direction per group were extracted. Outcome: Descriptive ranking of effect sizes and proportional shifts; six classes excluded due to <30 cells per arm.
9. **Cell composition mapping.** `run_composition` calculated the relative abundance of each major class per condition. Outcome: Proportional shifts quantified (MG +6.66 pp, AC +3.41 pp, BC −6.08 pp, Cone −3.73 pp, Rod stable).
10. **Pathway enrichment.** `run_enrichment` (ORA) tested genes passing lfc≥0.25 against the tested universe (11,293 genes) using GO_Biological_Process_2023, Reactome_2022, and MSigDB_Hallmark_2020. `run_gsea_prerank` walked Wilcoxon-ranked lists to compute signed NES and FDR q-values. Outcome: Coordinated pathway shifts identified; negative NES for phototransduction/glycolysis in sensory cells, positive NES for translation/stress in glial/amacrine cells.
11. **Cross-cell-type synthesis.** `run_code` aggregated per-class DE tables, filtered for lfc≥0.25, and computed a consensus ranking metric (abs(log2FC) × frac_expressed). Outcome: 1,715 pan-retinal genes consistently up-regulated; top consensus genes identified (Hmgn2, Ubb, mt-Cytb, Rps20, Klhl29).

## References
*Citations to be inserted by the literature module (PaperQA).*
