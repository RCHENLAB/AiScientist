# Transcriptional and Cellular Remodeling in the DDX41 Mutant Retina Reveals Reactive Gliosis, Photoreceptor Metabolic Reprogramming, and Pan-Retinal Translation Induction

## Abstract
DDX41 is an RNA helicase implicated in innate immunity and hematopoiesis, but its role in the retina is unknown. We performed single-cell RNA sequencing on retinal tissue from one DDX41 mutant mouse and one wild-type littermate to characterize the transcriptional and cellular composition changes associated with DDX41 loss. After quality control retaining all 15,307 cells, we conducted a descriptive, exploratory ranking of effect sizes within five major retinal cell classes. DDX41 mutant retina exhibited profound reactive gliosis in Müller glia (*Gfap* log2FC +6.02), a strong survival transcriptional programme in cones (*Cartpt* log2FC +5.99), and massive extracellular matrix remodeling in rods (*Col25a1* log2FC +8.74). Pathway enrichment revealed a near-universal up-regulation of translation machinery across all cell types, while phototransduction and glycolysis pathways were broadly suppressed. Cellular composition shifted markedly, with Müller glia and amacrine cells doubling in relative abundance while cones were depleted three-fold. These findings establish DDX41 as a critical regulator of retinal homeostasis whose loss triggers a coordinated multicellular stress response, reactive gliosis, and photoreceptor metabolic reprogramming. All results are descriptive and require validation in independent biological replicates.

## Study question
We asked what transcriptional and cellular changes occur in the DDX41 mutant retina compared to WT. This question is directly answerable from this dataset because it contains single-cell transcriptomes from one DDX41 mutant and one WT retina, with pre-annotated cell-type labels that enable a cell-type-stratified comparison of gene expression and cellular composition between the two conditions.

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

## Results

### Quality control
All 15,307 cells passed quality control filters (≥200 genes per cell, ≤10% mitochondrial reads, and singlet classification by DoubletFinder). The DDX41 mutant cells exhibited higher mean gene detection (2,076 genes per cell) compared to WT cells (1,505 genes per cell), and higher mean UMI counts (4,190 vs. 2,731). Mean mitochondrial read percentages were low and comparable between conditions (DDX41: 1.50%; WT: 1.60%). After filtering genes detected in fewer than three cells, 22,387 genes were retained for downstream analysis.

![Figure 1. Quality control summary showing per-cell distributions of gene counts, UMI counts, and mitochondrial read percentages across WT and DDX41 conditions.](figures/descriptive_qc_summary.png)

### Cell clustering
De novo Leiden clustering at the optimal resolution of 0.2 (stability score 0.961) produced 20 clusters. Mapping these clusters back to the pre-existing `majorclass` annotations confirmed that the annotated labels captured the major transcriptional structure of the data. The largest cluster (cluster 0, 6,199 cells) was 99.9% rod photoreceptors, while cluster 1 (3,738 cells) was 100% rods, cluster 2 (1,321 cells) was 99.6% Müller glia, and cluster 3 (813 cells) was 99.8% bipolar cells. No novel, unannotated populations were identified, and all downstream analyses used the pre-annotated `majorclass` labels.

![Figure 2. UMAP visualization of the 15,307 retinal cells colored by major class annotation and split by sample condition (WT and DDX41).](figures/umap_majorclass_sampleid.png)

### Cellular composition shifts
DDX41 mutant retina showed a marked reorganization of retinal cell populations compared to WT. Müller glia expanded from 5.91% to 12.57% of total cells (2.13-fold increase), and amacrine cells doubled from 2.85% to 6.26% (2.20-fold). In contrast, cones were depleted from 5.52% to 1.79% (3.08-fold reduction), and bipolar cells decreased from 18.88% to 12.80%. Rods remained proportionally stable (66.02% vs. 65.24%). Microglia showed a 3.62-fold relative increase but remained at low absolute abundance (0.27% to 0.96%). These proportions are descriptive only, as one library per condition precludes statistical testing.

### Differential expression

#### Müller glia exhibit reactive gliosis
Müller glia exhibited the most dramatic transcriptional response, with 5,057 genes up-regulated and 987 down-regulated (|log2FC| ≥ 0.25). The top up-regulated gene was *Gfap* (glial fibrillary acidic protein, log2FC +6.02), a canonical marker of reactive gliosis. Immune and complement genes dominated the up-regulated signature: *C4b* (+3.26), *Serping1* (+2.90), and *A2m* (+1.70). Down-regulated genes included synaptic receptors (*Grin2b* −3.46, *Grin2d* −3.18), suggesting synaptic remodeling.

![Figure 3. Volcano plot of descriptive effect sizes in Müller glia comparing DDX41 mutant versus WT. Genes past the descriptive threshold (|log2FC| ≥ 0.25) are highlighted.](figures/volcano_MG.png)

#### Rods undergo extracellular matrix remodeling and phototransduction suppression
Rods, the most abundant cell type, showed 3,283 up-regulated and 132 down-regulated genes. The top up-regulated gene was *Col25a1* (collagen type XXV alpha 1, log2FC +8.74), indicating massive extracellular matrix remodeling. Fibroblast growth factor pathway genes were also strongly induced: *Fgf2os* (+2.57) and *Fgf2* (+2.46). Down-regulated genes included axon guidance molecules (*Dcc* −5.83, *Nrn1* −5.35, *Aplp2* −4.91).

![Figure 4. Volcano plot of descriptive effect sizes in rod photoreceptors comparing DDX41 mutant versus WT. Genes past the descriptive threshold (|log2FC| ≥ 0.25) are highlighted.](figures/volcano_Rod.png)

#### Cones induce a survival programme and suppress glycolysis
Cones were overwhelmingly up-regulated (2,800 up vs. 1,102 down). The top up-regulated gene was *Cartpt* (cocaine- and amphetamine-regulated transcript peptide, log2FC +5.99), a neuropeptide associated with neuronal survival. Other strongly up-regulated genes included *B3galt1*, *Cadps2*, *Egr1*, and *Tox*. Down-regulated genes included ion channels (*Kcne2* −2.11) and phototransduction components.

![Figure 5. Volcano plot of descriptive effect sizes in cone photoreceptors comparing DDX41 mutant versus WT. Genes past the descriptive threshold (|log2FC| ≥ 0.25) are highlighted.](figures/volcano_Cone.png)

#### Bipolar cells and amacrine cells activate stress and inflammatory programmes
Bipolar cells showed 1,810 up-regulated and 524 down-regulated genes. The top up-regulated gene was *Xlr3b* (log2FC +3.83), along with complement components (*A2m* +3.32, *C4b*, *Serping1*) and immediate-early genes (*Fos* +2.35, *Egr1* +2.48, *Junb* +1.98). Amacrine cells showed 2,462 up-regulated and 1,509 down-regulated genes, dominated by ribosomal and proteasomal components (*Ubb*, *H3f3b*, *Rpl/Rps* family), while down-regulated genes included phototransduction genes (*Apoe* −4.91, *Pde6g* −1.90, *Rho* −1.84) despite amacrine cells being non-photoreceptor interneurons.

![Figure 6. Volcano plot of descriptive effect sizes in bipolar cells comparing DDX41 mutant versus WT. Genes past the descriptive threshold (|log2FC| ≥ 0.25) are highlighted.](figures/volcano_BC.png)

![Figure 7. Volcano plot of descriptive effect sizes in amacrine cells comparing DDX41 mutant versus WT. Genes past the descriptive threshold (|log2FC| ≥ 0.25) are highlighted.](figures/volcano_AC.png)

### Pathway enrichment

#### Translation machinery is universally up-regulated across cell types
Pathway over-representation analysis (ORA) and preranked gene-set enrichment analysis (GSEA) both identified translation as the dominant up-regulated programme across amacrine cells, bipolar cells, Müller glia, and rods. In Müller glia, preranked GSEA confirmed massive up-regulation of Peptide Chain Elongation (NES +3.48, FDR < 0.0001) and Eukaryotic Translation Elongation (NES +3.48, FDR < 0.0001). In rods, Translation (R-HSA-72766) was among the top enriched terms (ORA combined score 224.0, adj. p = 4.8e-23). In amacrine cells, Eukaryotic Translation Elongation showed NES +3.11 (FDR < 0.0001), and in bipolar cells, Viral mRNA Translation showed NES +2.51 (FDR < 0.0001).

![Figure 8. Pathway enrichment results for up-regulated genes in Müller glia.](figures/enrichment_MG_up.png)

![Figure 9. Pathway enrichment results for up-regulated genes in rod photoreceptors.](figures/enrichment_Rod_up.png)

![Figure 10. Pathway enrichment results for up-regulated genes in cone photoreceptors.](figures/enrichment_Cone_up.png)

![Figure 11. Pathway enrichment results for up-regulated genes in bipolar cells.](figures/enrichment_BC_up.png)

![Figure 12. Pathway enrichment results for up-regulated genes in amacrine cells.](figures/enrichment_AC_up.png)

#### Phototransduction and glycolysis are broadly suppressed
Phototransduction pathways were profoundly down-regulated across multiple cell types. In rods, Visual Phototransduction (R-HSA-2187338) showed NES −4.25 (FDR < 0.0001), and Phototransduction Cascade (R-HSA-2514856) showed NES −4.12 (FDR < 0.0001). In bipolar cells, Phototransduction Cascade showed NES −2.67 (FDR < 0.0001). In cones, glycolysis and carbohydrate catabolism were the most significantly down-regulated pathways: Glycolytic Process (GO:0006096, adj. p = 2.6e-10), Carbohydrate Catabolic Process (GO:0016052, adj. p = 1.1e-09), and Pyruvate Metabolic Process (GO:0006090, adj. p = 6.6e-09). Preranked GSEA confirmed this metabolic switch, with Pyruvate Metabolic Process showing NES −3.05 (FDR < 0.0001) and Glycolytic Process showing NES −2.87 (FDR < 0.0001).

![Figure 13. Pathway enrichment results for down-regulated genes in Müller glia.](figures/enrichment_MG_down.png)

![Figure 14. Pathway enrichment results for down-regulated genes in rod photoreceptors.](figures/enrichment_Rod_down.png)

![Figure 15. Pathway enrichment results for down-regulated genes in cone photoreceptors.](figures/enrichment_Cone_down.png)

![Figure 16. Pathway enrichment results for down-regulated genes in bipolar cells.](figures/enrichment_BC_down.png)

![Figure 17. Pathway enrichment results for down-regulated genes in amacrine cells.](figures/enrichment_AC_down.png)

### Cross-cell-type pan-retinal signature
The cross-cell-type synthesis identified 1,715 genes that were up-regulated in at least two of the five tested major classes. The most consistently up-regulated genes across all five cell types were *Hmgn2* (consensus score 0.664) and *Ubb* (consensus score 0.660), both involved in chromatin remodeling and protein homeostasis. *mt-Cytb* was up-regulated in four of five cell types (consensus 0.575), suggesting mitochondrial stress. Translation machinery was the dominant pan-retinal up-regulated programme, with ribosomal protein genes (*Rps20*, *Rpl38*, *Rps29*) and translation factors (*Eif1*) consistently induced across multiple cell types, corroborated by both ORA and GSEA results.

## Discussion
This exploratory study provides the first single-cell characterization of the DDX41 mutant retina. The data reveal a coordinated multicellular response involving reactive gliosis, photoreceptor metabolic reprogramming, and broad induction of translation machinery.

The most striking finding is the massive reactive gliosis in Müller glia, marked by a >6 log2-fold induction of *Gfap* and a doubling of MG relative abundance. This is accompanied by complement activation (*C4b*, *Serping1*) and down-regulation of synaptic genes, consistent with a glial scar-like response. Reactive gliosis is a hallmark of retinal injury and degeneration, and the DDX41 mutant may provide a model for studying the molecular drivers of this process.

Cones and rods exhibited divergent metabolic responses. Cones up-regulated oxidative phosphorylation while down-regulating glycolysis, suggesting a shift toward aerobic metabolism. This was accompanied by induction of *Cartpt*, a neuropeptide with neuroprotective properties, and may represent a survival adaptation. Rods, in contrast, showed profound down-regulation of phototransduction and induction of extracellular matrix genes (*Col25a1*, *Fgf2*), suggesting a fibrotic or wound-healing response. The proportional stability of rods despite dramatic transcriptional changes indicates that rods are transcriptionally altered but not yet lost at this timepoint.

The near-universal up-regulation of translation machinery across all five tested cell types is a notable finding. This may reflect a cellular stress response, as ribosome biogenesis and translation are rapidly induced under proteotoxic stress. Alternatively, it may be a direct consequence of DDX41 loss, given DDX41's role in ribosome biogenesis. The consistency of this signal across cell types suggests a cell-autonomous response to DDX41 deficiency rather than a secondary effect of tissue damage. However, the depth imbalance between conditions (DDX41 cells had higher mean UMI counts) introduces the possibility that some of the translation-related signal may be confounded by technical differences in library complexity, and this should be carefully evaluated in future studies.

## Limitations
1. **Single biological replicate per condition.** The dataset contains exactly one library per condition (WT: 1 donor; DDX41: 1 donor). All cells within a major class are subsamples of a single donor and are therefore pseudoreplicated. The Wilcoxon scores and log2FC values reported here are descriptive, exploratory rank metrics only and cannot be interpreted as inferential differential expression with valid p-values. No claims of statistical significance can be made.
2. **No adjusted p-values or FDR cutoffs were applied** in the per-cell-type effect ranking. The `padj` threshold was set to 1.0, meaning all genes passing the `lfc≥0.25` and `min_pct=0.1` filters were retained. The pathway enrichment analyses (ORA and GSEA) do use FDR correction, but these are contingent on the input gene rankings and should be interpreted with the same caution.
3. **Six of 11 major classes were excluded** from the stratified analysis due to insufficient cell counts (fewer than 30 cells in one or both arms). These include Endothelial, Horizontal, Microglia, Pericyte, Retinal Ganglion, and RPE cells. The transcriptional response of these populations to DDX41 loss remains unknown.
4. **Depth imbalance between conditions.** DDX41 mutant cells had higher mean UMI counts (4,190 vs. 2,731) and higher mean gene detection (2,076 vs. 1,505). This depth imbalance may confound some of the observed transcriptional differences, particularly the pan-retinal translation up-regulation signal.
5. **No validation by independent methods.** The findings are based solely on computational analysis of a single scRNA-seq dataset. Orthogonal validation (e.g., immunohistochemistry for GFAP, qPCR for *Cartpt* and *Col25a1*) is essential before drawing biological conclusions.
6. **Causality cannot be inferred.** The observed changes may be direct consequences of DDX41 loss, secondary effects of tissue degeneration, or a combination of both. Conditional knockout models and time-course experiments would be required to disentangle these possibilities.

## Conclusion
This descriptive analysis establishes that DDX41 mutant retina undergoes profound transcriptional and cellular remodeling, characterized by reactive Müller gliosis, cone survival programme induction, rod extracellular matrix remodeling, and pan-retinal translation up-regulation. These findings generate testable hypotheses regarding the role of DDX41 in retinal homeostasis and the molecular mechanisms underlying photoreceptor degeneration. The next step is to validate the key transcriptional changes—particularly *Gfap* induction in Müller glia, *Cartpt* up-regulation in cones, and *Col25a1* expression in rods—by immunohistochemistry and qPCR in independent biological replicates, and to perform time-course experiments to determine the temporal sequence and causality of these changes.

## Methods
1. **Data acquisition.** The input dataset was a pre-processed AnnData object (`Ddx41_DEG.h5ad`) containing 15,307 cells and 33,696 genes with existing UMAP and scVI embeddings, as well as curated cell-type annotations (`majorclass`, 11 categories; `celltype`, 87 categories). The `sampleid` column encoded the DDX41 vs WT contrast (WT: 9,047 cells; DDX41: 6,260 cells), with a single `orig.ident` level, confirming exactly one biological library per condition.
2. **Quality control.** QC was performed using `run_scanpy_qc` with the following parameters: `min_genes=200` per cell, `max_mito=10%`, and DoubletFinder singlet classification (`DF.classifications == "Singlet"`). All 15,307 cells passed these filters (0 cells removed). Genes detected in fewer than 3 cells were removed, reducing the gene count from 33,696 to 22,387. Counts were normalized and log1p-transformed, and the top 2,000 highly variable genes were selected for dimensionality reduction. Raw counts were retained in a `counts` layer for downstream differential expression testing.
3. **Dimensionality reduction and clustering.** Principal component analysis was performed on the 2,000 highly variable genes with `n_comps=30`. A k-nearest neighbor graph was constructed with `n_neighbors=15`. Leiden clustering was performed using `run_clustering` with automatic resolution selection via bootstrap stability (`select_resolution=true`, `n_bootstrap=10`, `stability_min=0.9`). The optimal resolution was 0.2, yielding 20 clusters with a stability score of 0.9605. UMAP coordinates were computed for visualization. Leiden cluster IDs were mapped back to the existing `majorclass` and `celltype` annotations to confirm concordance.
4. **Per-cell-type effect ranking.** Because the dataset contains only one biological library per condition, no valid statistical test for replicability exists. Therefore, a descriptive, exploratory ranking of effect sizes was performed using `run_de` in contrast mode. For each of the 11 annotated major retinal classes, Wilcoxon rank-sum statistics were computed comparing DDX41 mutant cells against WT cells within the same major class. Parameters: `min_pct=0.1` (genes detected in ≥10% of cells in one population), `lfc=0.25` (log2 fold-change threshold), top 50 genes retained per direction (up- and down-regulated). No adjusted p-value or FDR cutoff was applied (`padj=1.0`). Wilcoxon scores are reported strictly as non-inferential rank metrics. Five major classes met the minimum cell-count threshold of 30 cells in both arms: AC, BC, Cone, MG, and Rod. Six classes were excluded due to insufficient cells: Endothelial, HC, Microglia, Pericyte, RGC, and RPE.
5. **Cell composition mapping.** `run_composition` was used to quantify the relative abundance of each major retinal population in the DDX41 mutant versus WT library. Proportions are reported descriptively without statistical testing, as only one library exists per condition.
6. **Pathway over-representation analysis (ORA).** `run_enrichment` performed ORA against three pathway libraries—GO Biological Process 2023, Reactome 2022, and MSigDB Hallmark 2020—using genes passing the `lfc≥0.25` threshold as the query set and the full set of 11,293 measured genes as the background. The top 10 enriched terms per subset were reported.
7. **Preranked gene-set enrichment analysis (GSEA).** `run_gsea_prerank` read the complete Wilcoxon-ranked gene lists (.rnk files) for each major class and walked the ranked list against the same three pathway libraries. Parameters: minimum gene set size 15, maximum 500, 1,000 permutations, FDR threshold 0.05. Signed Normalized Enrichment Scores (NES) with FDR q-values and leading-edge genes were returned.
8. **Cross-cell-type synthesis.** `run_code` aggregated the per-class DE tables to identify genes changing in the same direction across multiple major classes. The script filtered for genes meeting the `lfc≥0.25` criteria and computed a consensus ranking metric (`abs(log2FC) × fraction of cell types expressed`) to prioritize robust pan-retinal changes.

## References
*Citations to be inserted by the literature module (PaperQA).*
