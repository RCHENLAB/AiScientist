# Transcriptional Reorganization and Reactive Gliosis in the DDX41 Mutant Retina

## Abstract
The DDX41 gene encodes a DEAD-box RNA helicase implicated in germline development and hematopoiesis, yet its role in somatic tissue homeostasis remains poorly characterized. Here, we present a single-cell RNA sequencing analysis of the DDX41 mutant retina compared to wild-type (WT) controls to define cell-type-specific transcriptional responses. The dataset comprises 15,307 cells from one WT and one DDX41 mutant library. Quality control retained 15,307 singlet cells (9,047 WT, 6,260 DDX41) and 22,387 genes. Differential expression analysis, stratified by major retinal cell classes, revealed that DDX41 mutation drives a multifaceted response characterized by reactive gliosis, immune activation, and metabolic reprogramming. Müller Glia (MG) and Amacrine cells (AC) expanded in proportion and up-regulated translation and stress-response pathways. Conversely, photoreceptor-adjacent Bipolar cells (BC) and Cones showed contraction and down-regulation of sensory perception pathways, while Rods exhibited a dramatic extracellular matrix and injury-response program. These descriptive findings highlight a robust neuroinflammatory and glial response to DDX41 loss, providing a baseline for future mechanistic studies.

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
|---|---:|---:|---:|
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
This dataset was generated to determine how DDX41 mutation alters the transcriptional landscape and cellular composition of the retina. Specifically, we asked which retinal cell types undergo significant changes in gene expression and proportion, and what biological pathways are enriched in these changes. This question is answerable because the single-cell resolution allows for the dissection of cell-type-specific responses that would be obscured in bulk tissue analysis, despite the limitation of having only one biological replicate per condition.

## Results

### Quality control
The final dataset comprised 15,307 high-quality singlet cells (9,047 WT, 6,260 DDX41) and 22,387 genes. All cells passed singlet classification, and no cells were removed by mitochondrial percentage or gene count filters. DDX41 mutant cells exhibited a higher mean number of features per cell (2,075.8) compared to WT cells (1,504.6), suggesting a global increase in transcriptional activity or complexity in the mutant retina.

Figure 1. Quality control metrics summarizing gene counts, mitochondrial percentage, and cell classification for WT and DDX41 mutant retinas.
![Figure 1. Quality control metrics summarizing gene counts, mitochondrial percentage, and cell classification for WT and DDX41 mutant retinas.](figures/descriptive_qc_summary.png)

### Cell clustering and composition
Unsupervised clustering identified 20 distinct clusters, which were mapped to 11 major retinal cell classes. Descriptive analysis of cellular proportions revealed significant reorganization in the DDX41 mutant retina. Müller Glia (MG) and Amacrine cells (AC) increased in proportion (MG: 5.91% to 12.57%; AC: 2.85% to 6.26%), while Bipolar (BC) and Cone cells decreased (BC: 18.88% to 12.80%; Cone: 5.52% to 1.79%). Rod proportions remained stable (~66%). Microglia also showed a relative increase, though absolute numbers remained low.

Figure 2. UMAP visualization of 20 cell clusters colored by major cell class (left) and sample ID (right), illustrating the distribution of WT and DDX41 mutant cells across retinal lineages.
![Figure 2. UMAP visualization of 20 cell clusters colored by major cell class (left) and sample ID (right), illustrating the distribution of WT and DDX41 mutant cells across retinal lineages.](figures/umap_majorclass_sampleid.png)

### Differential expression and pathway enrichment
Differential expression analysis was restricted to five major classes with sufficient cell counts: AC, BC, Cone, MG, and Rod. Due to the single-library-per-condition design, results are reported as descriptive rankings of effect sizes (log2 fold-changes) and Wilcoxon scores.

**Müller Glia (MG)** exhibited the most dramatic transcriptional response, indicative of reactive gliosis. *Gfap* was the top up-regulated gene (log2FC = +6.02), alongside immune-related genes *C4b* (log2FC = +3.26) and *Serping1* (log2FC = +2.90). Down-regulated genes included synaptic receptors *Grin2b* and *Grin2d*. Pathway enrichment revealed up-regulation of translation and rRNA processing, and down-regulation of nervous system development and synapse assembly.

Figure 3. Enrichment analysis for up-regulated genes in Müller Glia, highlighting translation and rRNA processing pathways.
![Figure 3. Enrichment analysis for up-regulated genes in Müller Glia, highlighting translation and rRNA processing pathways.](figures/enrichment_MG_up.png)

Figure 4. Enrichment analysis for down-regulated genes in Müller Glia, highlighting nervous system development and synapse assembly pathways.
![Figure 4. Enrichment analysis for down-regulated genes in Müller Glia, highlighting nervous system development and synapse assembly pathways.](figures/enrichment_MG_down.png)

**Bipolar cells (BC)** showed signs of acute activation and immune response. *Xlr3b* (log2FC = +3.83) and *A2m* (log2FC = +3.32) were up-regulated, along with immediate-early genes (*Fos*, *Egr1*, *Junb*). Phototransduction cascade pathways were significantly down-regulated (NES = -2.67).

Figure 5. Enrichment analysis for up-regulated genes in Bipolar cells, highlighting immune response and immediate-early gene pathways.
![Figure 5. Enrichment analysis for up-regulated genes in Bipolar cells, highlighting immune response and immediate-early gene pathways.](figures/enrichment_BC_up.png)

Figure 6. Enrichment analysis for down-regulated genes in Bipolar cells, highlighting phototransduction and sensory perception pathways.
![Figure 6. Enrichment analysis for down-regulated genes in Bipolar cells, highlighting phototransduction and sensory perception pathways.](figures/enrichment_BC_down.png)

**Cone cells** displayed a strong up-regulated survival or compensatory program. *Cartpt* was the strongest signal (log2FC = +5.99). Enrichment analysis identified up-regulation of oxidative phosphorylation and translesion synthesis, while glycolytic and hypoxia-related pathways were down-regulated.

Figure 7. Enrichment analysis for up-regulated genes in Cone cells, highlighting oxidative phosphorylation and translesion synthesis.
![Figure 7. Enrichment analysis for up-regulated genes in Cone cells, highlighting oxidative phosphorylation and translesion synthesis.](figures/enrichment_Cone_up.png)

Figure 8. Enrichment analysis for down-regulated genes in Cone cells, highlighting glycolytic and hypoxia-related pathways.
![Figure 8. Enrichment analysis for down-regulated genes in Cone cells, highlighting glycolytic and hypoxia-related pathways.](figures/enrichment_Cone_down.png)

**Rod cells** underwent a dramatic extracellular matrix and injury-response program. *Col25a1* (log2FC = +8.74) was the top up-regulated gene. Visual phototransduction pathways were massively down-regulated (NES = -4.25), while mitochondrial electron transport was up-regulated.

Figure 9. Enrichment analysis for up-regulated genes in Rod cells, highlighting extracellular matrix organization and mitochondrial electron transport.
![Figure 9. Enrichment analysis for up-regulated genes in Rod cells, highlighting extracellular matrix organization and mitochondrial electron transport.](figures/enrichment_Rod_up.png)

Figure 10. Enrichment analysis for down-regulated genes in Rod cells, highlighting visual phototransduction pathways.
![Figure 10. Enrichment analysis for down-regulated genes in Rod cells, highlighting visual phototransduction pathways.](figures/enrichment_Rod_down.png)

**Amacrine cells (AC)** showed unexpected down-regulation of phototransduction genes, suggesting a secondary effect from photoreceptor loss. Ribosomal and proteasomal genes were up-regulated, consistent with a stress response. Enrichment confirmed up-regulation of translation elongation and down-regulation of visual perception pathways.

Figure 11. Enrichment analysis for up-regulated genes in Amacrine cells, highlighting translation elongation and ribosomal pathways.
![Figure 11. Enrichment analysis for up-regulated genes in Amacrine cells, highlighting translation elongation and ribosomal pathways.](figures/enrichment_AC_up.png)

Figure 12. Enrichment analysis for down-regulated genes in Amacrine cells, highlighting visual perception and phototransduction pathways.
![Figure 12. Enrichment analysis for down-regulated genes in Amacrine cells, highlighting visual perception and phototransduction pathways.](figures/enrichment_AC_down.png)

### Cross-cell-type consensus
Analysis of genes changing direction across multiple major classes identified a pan-retinal stress and translational response. Genes such as *Hmgn2*, *Ubb*, and *mt-Cytb* were up-regulated across 4–5 major classes. Phototransduction-related genes were broadly down-regulated in AC, BC, Cone, and Rod.

## Discussion
The DDX41 mutant retina exhibits a complex transcriptional landscape characterized by reactive gliosis, immune activation, and metabolic shifts. The expansion of Müller Glia and Amacrine cell proportions, coupled with the up-regulation of *Gfap* and immune markers (*C4b*, *A2m*), suggests a robust neuroinflammatory and glial response.

Conversely, photoreceptor-adjacent cells (BC, Cone) show contraction in proportion and down-regulation of sensory perception pathways. However, Cones mount a distinct up-regulated survival program (*Cartpt*, *Oxidative Phosphorylation*), while Rods exhibit a fibrotic/extracellular matrix response (*Col25a1*). The down-regulation of phototransduction pathways in ACs, despite their non-photoreceptor identity, implies a secondary signaling cascade triggered by photoreceptor stress.

A consistent theme across MG, BC, and AC is the up-regulation of **Translation** and **Peptide Chain Elongation** pathways. This may reflect a global cellular stress response or increased protein synthesis demands associated with gliosis and immune activation.

## Limitations
1.  **Single Library per Condition:** The analysis is based on one WT and one DDX41 library. No statistical tests for replicability or differential expression significance (FDR) are valid. All findings are descriptive effect sizes.
2.  **Cell Type Coverage:** Differential expression and enrichment analyses were restricted to AC, BC, Cone, MG, and Rod due to low cell counts in Endothelial, HC, Microglia, Pericyte, RGC, and RPE. Findings for these six classes are not reported.
3.  **Inference Disclaimer:** Wilcoxon scores and log2FC values are non-inferential rank metrics. Pathway enrichment results indicate association with expression programs, not necessarily causal pathway activity.

## Conclusion
DDX41 mutation drives a multifaceted retinal response involving reactive gliosis, immune activation, and metabolic reprogramming. While photoreceptor proportions remain stable, their transcriptional state is altered, and adjacent interneurons (AC, BC) show significant stress and immune signatures. These descriptive findings provide hypotheses for validating DDX41’s role in retinal homeostasis and stress response in future studies with replicated biological samples.

## Methods
1.  **Data Acquisition and Quality Control.** Raw count-like features were loaded from `Ddx41_DEG.h5ad`. Cells were filtered using `run_scanpy_qc` with thresholds: `min_genes=200`, `max_pct_mt=10`, and `Singlet` classification retained. 0 cells were removed. Initial dataset: 15,307 cells, 33,696 genes. Low-quality genes (detected in <3 cells) were removed, retaining 22,387 genes.
2.  **Highly Variable Gene Selection.** Top 2,000 highly variable genes (HVGs) were selected for downstream structure preservation.
3.  **Normalization.** Counts were log1p-transformed to mitigate sequencing depth effects.
4.  **Clustering.** Leiden clustering was performed using `run_clustering` on the top 30 principal components (`n_pcs=30`) and a neighborhood graph with 15 neighbors (`n_neighbors=15`). Optimal resolution was selected via bootstrap stability (`n_bootstrap=10`, `stability_min=0.9`), yielding resolution=0.2 and 20 clusters.
5.  **Dimensionality Reduction.** UMAP was generated for visualization of cell clusters.
6.  **Differential Expression.** `run_de` was used in contrast mode, comparing DDX41 mutant vs WT within each of 11 annotated major classes. Method: Wilcoxon rank-sum test (tie-corrected). Parameters: `min_pct=0.1`, `lfc=0.25`. Five classes met minimum cell count requirements (≥30 cells in both arms): AC, BC, Cone, MG, Rod. Six classes were excluded: Endothelial, HC, Microglia, Pericyte, RGC, RPE. No adjusted p-values were applied due to single-library limitation.
7.  **Cell Composition Mapping.** `run_composition` calculated the fraction of cells belonging to each major class per condition.
8.  **Pathway Enrichment (ORA).** `run_enrichment` performed over-representation analysis on genes with |log2FC| ≥ 0.25. Background: 11,293 genes tested. Gene sets: `GO_Biological_Process_2023`, `Reactome_2022`, `MSigDB_Hallmark_2020`.
9.  **Pathway Enrichment (GSEA).** `run_gsea_prerank` applied preranked GSEA to complete Wilcoxon-ranked gene lists. Ranking statistic: Wilcoxon Z-scores. Significance: FDR q-values < 0.05.
10. **Cross-Cell-Type Synthesis.** `run_code` aggregated per-class DE tables. Consensus ranking metric: `abs(log2FC) × frac_expressed`.

## References
*Citations to be inserted by the literature module (PaperQA).*
