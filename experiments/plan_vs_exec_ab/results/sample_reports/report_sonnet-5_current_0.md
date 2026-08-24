# Transcriptional and Compositional Remodeling of the Retina in a Single DDX41 Mutant Mouse: Neuroglial Expansion, Bipolar/Cone Contraction, and Coordinated Suppression of Glycolytic and Phototransduction Programs

## Abstract

DDX41 is implicated in RNA metabolism and innate immune signaling, but its role in retinal cell-type maintenance is uncharacterized. We analyzed a single-cell RNA-seq dataset comprising one WT retina library (9,047 cells) and one DDX41 mutant retina library (6,260 cells; 15,307 cells total after QC, 22,387 genes retained), profiled across 11 annotated major retinal cell classes. Because the design contains exactly one biological library per condition with no replicates, all comparisons — compositional, differential-expression, and pathway-level — were treated as descriptive/exploratory rather than statistically inferential. Leiden clustering (30 PCs, 15 neighbors, resolution 0.2, bootstrap stability 0.9605) yielded 20 clusters that closely tracked pre-existing majorclass/celltype annotations, which were used for all downstream analyses. Compositional comparison showed proportional expansion of Müller glia (MG, 5.9% to 12.6%) and amacrine cells (AC, 2.9% to 6.3%) in DDX41 relative to WT, contraction of bipolar cells (BC, 18.9% to 12.8%) and cones (5.5% to 1.8%), and a stable rod fraction (66.0% to 65.2%). Wilcoxon rank-based comparisons (five classes with ≥30 cells per arm: AC, BC, Cone, MG, Rod) identified Gfap (+6.02 log2FC) as the top up-ranked gene in MG, and preranked GSEA converged with over-representation analysis on down-regulation of glycolysis/hypoxia and phototransduction programs in Cone and Rod, alongside up-regulation of translation-associated programs in AC, BC, and MG. These descriptive, single-library findings generate testable hypotheses about neuroglial reorganization and metabolic/phototransduction suppression in DDX41 mutant retina, but require replicated cohorts for statistical confirmation.

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

This dataset pairs one DDX41 mutant retina library against one WT retina library, each pre-annotated into 11 major cell classes, raising the question of what compositional and transcriptional differences distinguish the two conditions at cell-type resolution. Because both libraries carry full-transcriptome single-cell profiles with existing majorclass/celltype labels, this design is directly answerable for descriptive comparisons of cell-type proportions and within-class expression ranking, while explicitly not supporting population-level statistical inference given the absence of biological replicates.

## Results

### Quality control

Both libraries passed QC as single high-confidence singlet populations, with no cells removed at the filtering thresholds applied, but retained a depth difference between conditions that must be considered when interpreting downstream expression comparisons. All 15,307 cells in the combined object were classified as singlets by doublet-detection classification, and no cells were excluded on that basis. Applying the QC thresholds (≥200 genes/cell, ≤10% mitochondrial reads) removed 0% of cells (15,307 of 15,307 retained), while gene filtering (≥3 cells/gene equivalent) reduced the feature space from 33,696 to 22,387 genes, with 13 mitochondrial genes identified for percent-mt calculation. Descriptive QC metrics, however, revealed that DDX41 cells had systematically higher sequencing depth and complexity than WT cells (mean nFeature_RNA 2075.8 vs 1504.6; median 1776.5 vs 1194.0; mean nCount_RNA 4189.96 vs 2730.61), a depth imbalance that constitutes a technical confound for any same-direction expression shifts observed between conditions, particularly for translation- and ribosome-associated genes (Figure 1). Per-cell scatter and violin plots of gene counts and mitochondrial content did not indicate additional need for filtering beyond the applied thresholds (Figure 2, Figure 3, Figure 4).

![Figure 1. Descriptive QC summary comparing DDX41 and WT libraries across nFeature_RNA, nCount_RNA, and percent mitochondrial reads.](figures/descriptive_qc_summary.png)

![Figure 2. Per-cell scatter plot of gene counts (nFeature_RNA) versus total counts (nCount_RNA), used to assess QC filtering thresholds.](figures/scatter_qc_genes.png)

![Figure 3. Per-cell scatter plot of mitochondrial read percentage versus total counts, used to assess the ≤10% mitochondrial filtering threshold.](figures/scatter_qc_mt.png)

![Figure 4. Violin plots of QC metrics (nFeature_RNA, nCount_RNA, percent.mt) across the dataset.](figures/violin_qc_violin.png)

### Cell clustering

Unsupervised structure recapitulated the pre-existing cell-type annotation closely, supporting the use of majorclass/celltype labels — rather than anonymous Leiden IDs — for all downstream comparisons. Leiden clustering on 30 principal components with a 15-nearest-neighbor graph, swept across resolutions from 0.2 to 2.0 with bootstrap stability testing (10 bootstraps, stability_min = 0.9), identified resolution 0.2 as the most stable solution (stability 0.9605 ± 0.0081), yielding 20 clusters ranging from 15 to 6,199 cells (Figure 5). Higher resolutions produced more clusters but markedly lower stability (e.g., resolution 1.0: 34 clusters, stability 0.599), and no cluster was flagged as unstable at the chosen resolution. Cross-tabulation against existing annotation confirmed strong correspondence: the largest cluster (n=6,199) was 99.9% Rod, the second largest (n=3,738) was 100% Rod, cluster 2 (n=1,321) was 99.6% MG, cluster 3 (n=813) was 99.8% BC, and cluster 4 (n=607) was 100% Cone. Because the dataset already carried robust majorclass and celltype labels, these annotations — not Leiden cluster IDs — were used for differential expression, enrichment, and cross-cell-type synthesis. A combined UMAP colored by majorclass and by sampleid is shown in Figure 6.

![Figure 5. UMAP embedding of all cells colored by Leiden cluster assignment at the selected resolution (0.2; 20 clusters).](figures/umap_clusters.png)

![Figure 6. UMAP embedding colored by pre-existing majorclass annotation (left/panel) and by sampleid (WT vs DDX41; right/panel), showing cluster-to-annotation correspondence and the distribution of each condition across cell types.](figures/umap_majorclass_sampleid.png)

Highly variable gene selection supporting the clustering and UMAP embedding is summarized in Figure 7.

![Figure 7. Mean-dispersion plot of highly variable genes (n=2,000) used for dimensionality reduction and clustering.](figures/filter_genes_dispersion_hvg.png)

### Cell-type annotation

No independent reference-based or foundation-model annotation tool (e.g., scGPT) was run in this analysis; cell-type labels used throughout were the dataset's existing majorclass/celltype annotations, validated post hoc by the Leiden cluster reconciliation described above. This reconciliation, rather than a foundation-model prediction, is the annotation evidence reported here.

### Compositional shifts

The dominant finding of this analysis is a reorganization of cell-type proportions between conditions, with the largest absolute gains in glial and interneuron classes and the largest absolute loss in bipolar cells. Of the 11 annotated major classes, MG proportion more than doubled (5.914% in WT to 12.572% in DDX41, 2.13-fold, +6.66 percentage points) and AC proportion also more than doubled (2.852% to 6.262%, 2.20-fold, +3.41 pp). Microglia and pericyte proportions showed proportionally large fold changes (3.62-fold and 2.91-fold respectively) but from very small absolute baselines (Microglia 0.265% to 0.958%; Pericyte 0.022% to 0.064%). In contrast, BC proportion contracted from 18.879% to 12.796% (0.68-fold, −6.08 pp, the largest absolute loss), Cone proportion contracted roughly three-fold (5.516% to 1.789%), and Endothelial proportion decreased (0.298% to 0.112%). Rod proportion, the dominant retinal class in both conditions, remained essentially stable (66.022% vs 65.240%). This pattern is consistent with a working hypothesis of neuroglial/interneuron expansion occurring alongside bipolar and cone contraction in DDX41 mutant retina, though as a one-library-per-condition comparison it cannot exclude sampling or dissociation differences as contributing factors.

### Differential expression

Within the five major classes with sufficient cells in both arms (AC, BC, Cone, MG, Rod), Wilcoxon rank-based comparisons produced descriptive gene rankings rather than statistically significant calls, since no FDR threshold was applied (padj = 1.0) given the single-library design.

| Class | Top up-ranked gene (log2FC) | Top down-ranked gene (log2FC) | Genes |log2FC|≥1 (up/down of top 50) |
|---|---|---|---|
| MG | Gfap (+6.02) | Grin2b/Grin2d (down) | 45 / 24 |
| Rod | Col25a1 (+8.74) | Dcc (−5.83) | 49 / 12 |
| Cone | Cartpt (+5.99) | Kcne2 (−2.11) | 47 / 16 |
| AC | Angpt1 (+2.58) | Apoe (−4.91) | 9 / 38 |
| BC | Xlr3b (+3.83) | Sorcs3 (−2.19) | 24 / 26 |

In MG, Gfap showed the largest positive log2 fold-change among top-ranked genes (Wilcoxon score 19.9), alongside up-ranked C4b, Serping1, and A2m (Figure 8). In Rod, Col25a1 and Fgf2/Fgf2os were the most positive-ranked genes, with Dcc, Nrn1, and Aplp2 among the most negative (Figure 9). In Cone, Cartpt was the top positive-ranked gene and Kcne2 and Kcnma1 among the most negative (Figure 10). In AC, Angpt1 ranked highest positively and Apoe most negatively, alongside down-ranked phototransduction transcripts (Pde6g, Rho, Gnat1) despite AC being a non-photoreceptor class (Figure 11). In BC, Xlr3b ranked highest positively (alongside A2m and immediate-early genes Fos and Egr1) and Sorcs3 most negatively (Figure 12). Cross-referencing top-ranked genes across the five tested classes identified genes changing in the same direction in multiple classes — Gfap up in AC and MG, C4b up in BC and MG, A2m up in AC, BC, and MG — and a consensus-ranking aggregation (abs(log2FC) × frac_expressed) across all five tested classes flagged Hmgn2 (max log2FC 1.61, consensus 0.664) and Ubb (max log2FC 1.05, consensus 0.66) as up-ranked in all 5 groups, with mt-Cytb and Klhl29 up-ranked in 4 of 5. A total of 1,715 genes met the lfc≥0.25 threshold as up-ranked in at least one class in this aggregated analysis. Six classes (Endothelial, HC, Microglia, Pericyte, RGC, RPE) fell below the 30-cell-per-arm minimum in at least one condition and were not tested; no DE, enrichment, or GSEA results exist for these classes, and only their compositional percentages are reported.

![Figure 8. Volcano plot of Müller glia (MG) DDX41-vs-WT Wilcoxon comparison; x-axis log2 fold-change, y-axis significance, highlighting Gfap, C4b, Serping1, and A2m.](figures/volcano_MG.png)

![Figure 9. Volcano plot of Rod DDX41-vs-WT Wilcoxon comparison; x-axis log2 fold-change, y-axis significance, highlighting Col25a1, Fgf2/Fgf2os, and Dcc.](figures/volcano_Rod.png)

![Figure 10. Volcano plot of Cone DDX41-vs-WT Wilcoxon comparison; x-axis log2 fold-change, y-axis significance, highlighting Cartpt, Kcne2, and Kcnma1.](figures/volcano_Cone.png)

![Figure 11. Volcano plot of amacrine cell (AC) DDX41-vs-WT Wilcoxon comparison; x-axis log2 fold-change, y-axis significance, highlighting Angpt1, Apoe, and down-ranked phototransduction transcripts (Pde6g, Rho, Gnat1).](figures/volcano_AC.png)

![Figure 12. Volcano plot of bipolar cell (BC) DDX41-vs-WT Wilcoxon comparison; x-axis log2 fold-change, y-axis significance, highlighting Xlr3b, A2m, Fos, Egr1, and Sorcs3.](figures/volcano_BC.png)

### Pathway enrichment

Both over-representation analysis (ORA) of top-ranked genes and preranked GSEA of full Wilcoxon-ranked lists — tested against a background of 11,293 measured genes across GO Biological Process 2023, Reactome 2022, and MSigDB Hallmark 2020 — converged on two coordinated programs: suppression of glycolytic/hypoxia metabolism and phototransduction in photoreceptors, and induction of translation-associated processes in AC, BC, and MG. In Cone, the down-ranked gene set was dominated by glycolysis and carbohydrate catabolism terms (Glycolytic Process GO:0006096, adjusted p = 2.6e-10; Carbohydrate Catabolic Process GO:0016052, adjusted p = 1.1e-09; Pyruvate Metabolic Process GO:0006090, adjusted p = 6.6e-09; Hypoxia, adjusted p = 1.4e-08) (Figure 13), and preranked GSEA confirmed this direction with negative NES for the same terms (Pyruvate Metabolic Process NES = −3.05; Carbohydrate Catabolic Process NES = −2.90; Glycolytic Process NES = −2.87; Hypoxia NES = −2.59) plus additional down-NES for Visual Perception (GO:0007601, NES = −2.29, FDR = 4.0e-03) and Sensory Perception Of Light Stimulus (GO:0050953, NES = −2.12, FDR = 1.2e-02).

In Rod, preranked GSEA showed the strongest down-NES terms of any class, dominated by phototransduction (Visual Phototransduction R-HSA-2187338, NES = −4.25; Phototransduction Cascade R-HSA-2514856, NES = −4.12; Inactivation, Recovery And Regulation Of Phototransduction Cascade R-HSA-2514859, NES = −4.00) together with Hypoxia (NES = −3.33) and glycolysis-related terms (Pyruvate Metabolic Process, NES = −2.69) (Figure 14). ORA of Rod up-ranked genes instead returned broad R
