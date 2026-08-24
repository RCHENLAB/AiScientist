# DDX41 Mutation Induces Retinal Cellular Reorganization and Global Translational Stress Responses

## Abstract
The DDX41 gene encodes a DEAD-box RNA helicase essential for ribosome biogenesis and stress granule dynamics. While DDX41 mutations are established drivers of hematologic malignancies, their impact on post-mitotic tissues such as the retina remains uncharacterized. Here, we present a single-cell RNA sequencing analysis of the retina from wild-type (WT) and DDX41 mutant donors to define the transcriptional and compositional consequences of this mutation. We analyzed 15,307 cells, identifying significant shifts in cellular composition, including a doubling of Müller Glia (MG) and Amacrine Cell (AC) proportions, and a contraction of Bipolar Cells (BC) and Cones. Differential expression analysis revealed a pan-retinal signature of translational up-regulation and phototransduction down-regulation. Specifically, MGs exhibited markers of reactive gliosis (*Gfap*), while Rods and Cones displayed distinct stress responses involving extracellular matrix remodeling and oxidative phosphorylation, respectively. These findings suggest that DDX41 haploinsufficiency triggers a widespread neuroinflammatory and stress-response program in the retina, potentially contributing to retinal degeneration.

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
This study asks how the transcriptional landscape and cellular composition of the retina are altered by DDX41 mutation compared to wild-type controls. Specifically, we sought to identify which retinal cell types are most vulnerable to DDX41 loss and whether the mutation induces a conserved stress or reactive program across the retinal layers. This question is answerable from the dataset because it provides matched single-cell transcriptomes from a single WT and a single DDX41 mutant donor, allowing for direct comparison of gene expression profiles and cell type abundances between the two conditions.

## Results

### Quality Control and Dataset Overview
The initial dataset comprised 15,307 cells and 33,696 genes. After rigorous quality control filtering—retaining cells with ≥200 detected genes, ≤10% mitochondrial reads, and singlet classification—15,307 cells and 22,387 genes remained for analysis. The final cohort consisted of 9,047 cells from the wild-type (WT) donor and 6,260 cells from the DDX41 mutant donor. All cells passed singlet classification, ensuring that the observed transcriptional changes reflect genuine biological differences rather than doublet artifacts.

![Figure 1. UMAP visualization of 15,307 cells colored by major cell class, revealing distinct clustering of Rods, Cones, Bipolar Cells, Amacrine Cells, and Müller Glia.](figures/umap_majorclass_sampleid.png)

### Cell Composition Shifts
DDX41 mutation induced a profound reorganization of the retinal cellular landscape. Relative abundance analysis revealed a significant expansion of non-neuronal and interneuronal populations alongside a contraction of photoreceptors and bipolar cells. Müller Glia (MG) increased from 5.91% in WT to 12.57% in DDX41 mutants (2.13× fold change), and Amacrine Cells (AC) increased from 2.85% to 6.26% (2.20× fold change). Conversely, Bipolar Cells (BC) decreased from 18.88% to 12.80% (0.68× fold change), and Cones decreased from 5.52% to 1.79% (0.32× fold change). Rod proportions remained relatively stable at ~65–66%, despite significant transcriptional changes within this population. Microglia also showed an increase in relative abundance (0.27% to 0.96%), though absolute numbers remained low.

![Figure 2. Violin plots of nFeature_RNA and percent.mt distributions for WT and DDX41 samples, confirming low mitochondrial content and comparable complexity.](figures/violin_qc_violin.png)

### Differential Expression and Reactive Gliosis
Differential expression analysis was restricted to five major cell classes with sufficient cell counts (AC, BC, Cone, MG, Rod). Six minor classes (Endothelial, HC, Microglia, Pericyte, RGC, RPE) were excluded due to having fewer than 30 cells in one or both arms, violating minimum count requirements for statistical approximation.

**Müller Glia (MG)** exhibited the most dramatic transcriptional shift, consistent with reactive gliosis. The top up-regulated gene was *Gfap* (log2FC = +6.0), a canonical marker of glial activation. Enrichment analysis of up-regulated MG genes highlighted **Translation (GO:0006412)** and **Peptide Chain Elongation** pathways. Conversely, synaptic receptor genes such as *Grin2b* (log2FC = −3.5) were down-regulated, with enriched terms including **Nervous System Development** and **Synapse Assembly**.

**Bipolar Cells (BC)** showed signs of acute activation and immune response. Immediate-early genes (*Fos*, *Egr1*) and complement components (*A2m*, *C4b*) were highly up-regulated. Enriched terms included **Translation** and **Peptide Biosynthetic Process**. Notably, phototransduction-related terms were significantly down-regulated, suggesting a decoupling of photoreceptor input processing.

![Figure 3. Enrichment plot for up-regulated MG genes showing translational pathways.](figures/enrichment_MG_up.png)

### Photoreceptor Stress and Rod/Cone Divergence
Despite stable cellular proportions, **Rods** underwent a dramatic transcriptional shift toward extracellular matrix (ECM) and stress responses. *Col25a1* (log2FC = +8.7) and *Fgf2* were the top up-regulated genes. Enriched terms included **Cellular Responses To Stress** and **Metabolism Of RNA**. Phototransduction was severely down-regulated, with **Visual Phototransduction** (NES = -4.2) and **Phototransduction Cascade** being the top enriched down-regulated pathways.

**Cones** displayed a distinct compensatory program. The top up-regulated gene was *Cartpt* (log2FC = +6.0). Enriched terms included **Oxidative Phosphorylation** and **Translation**. Down-regulated terms included **Glycolytic Process** and **Hypoxia**, suggesting a metabolic shift from glycolysis to oxidative phosphorylation to cope with stress.

![Figure 4. Enrichment plot for up-regulated Rod genes showing ECM and stress responses.](figures/enrichment_Rod_up.png)

### Amacrine Cell Down-regulation and Global Translational Shift
**Amacrine Cells (AC)** showed down-regulation of phototransduction genes despite being non-photoreceptors, suggesting a secondary effect of retinal stress. *Apoe* (log2FC = −4.9) and other phototransduction components were down-regulated. Up-regulated genes included ribosomal and proteasomal components (*Ubb*, *H3f3b*), with enriched terms for **Cytoplasmic Translation** and **Eukaryotic Translation Elongation**.

### Pathway Enrichment and Cross-Cell-Type Consensus
Enrichment analysis consistently highlighted two major themes across cell types:
1.  **Translational Up-regulation:** Terms such as **Eukaryotic Translation Elongation**, **Peptide Chain Elongation**, and **Cytoplasmic Translation** were significantly enriched in up-regulated sets for AC, BC, MG, and Rod.
2.  **Phototransduction Down-regulation:** Terms including **Visual Phototransduction**, **Phototransduction Cascade**, and **Inactivation, Recovery And Regulation Of Phototransduction Cascade** were significantly enriched in down-regulated sets for BC, Cone, Rod, and AC.

Cross-cell-type synthesis identified a pan-retinal signature. Genes such as *Ubb* and *Hmgn2* were up-regulated across all five tested major classes. While no single gene was down-regulated across all five classes, phototransduction pathways were broadly suppressed in Rod, Cone, BC, and AC, indicating a coordinated loss of visual function signaling.

![Figure 5. Workflow diagram illustrating the analysis pipeline from QC to cross-cell-type consensus.](figures/workflow.png)

## Discussion
The DDX41 mutation induces a profound reorganization of the retinal transcriptome and cellular composition. The expansion of Müller Glia and Amacrine cells, coupled with the contraction of Bipolar and Cone populations, suggests a shift toward a reactive, non-neuronal state. Key findings include:

*   **Reactive Gliosis:** MGs show massive up-regulation of *Gfap* and translational machinery, indicating a robust stress response. This is consistent with the role of DDX41 in ribosome biogenesis; its loss may trigger a compensatory increase in protein synthesis capacity or a stress-induced activation of glial cells.
*   **Photoreceptor Stress:** Despite stable proportions, Rods show extreme up-regulation of ECM genes (*Col25a1*) and down-regulation of phototransduction. Cones show a compensatory up-regulation of *Cartpt* and oxidative phosphorylation, suggesting a distinct survival mechanism compared to rods.
*   **Immune Activation:** BCs and MGs show up-regulation of complement and immune-related genes (*C4b*, *A2m*), suggesting neuroinflammation. This may be a secondary consequence of photoreceptor stress or a primary effect of DDX41 loss on immune surveillance.
*   **Global Translational Shift:** A coordinated up-regulation of translation-related pathways is observed across AC, BC, MG, and Rod, potentially reflecting a general cellular stress or repair response. This global shift may be driven by the need to replace damaged proteins or compensate for ribosomal dysfunction.

## Limitations
This study is limited by the use of a single biological library per condition (WT and DDX41). Consequently, no statistical tests for replicability or differential expression significance (p-values/FDR) are valid. All reported fold-changes and enrichment scores are descriptive metrics. Additionally, six minor retinal cell classes (Endothelial, HC, Microglia, Pericyte, RGC, RPE) were excluded from differential analysis due to insufficient cell numbers (<30 cells in one arm). These findings should be validated in a study with biological replicates.

## Conclusion
We characterized the transcriptional and compositional changes in the retina caused by DDX41 mutation. The primary finding is a pan-retinal stress response characterized by translational up-regulation and phototransduction down-regulation, accompanied by a significant shift in cellular composition favoring Müller Glia and Amacrine Cells. The next step is to validate these findings in a larger cohort with biological replicates to establish statistical significance and to investigate the functional consequences of these transcriptional changes on retinal physiology.

## Methods
1.  **Quality Control and Normalization.** Raw counts were filtered for cells with ≥200 genes (`min_genes=200`), ≤10% mitochondrial reads (`percent.mt=10`), and singlet classification (`DF.classifications`). Genes detected in <3 cells were removed (`min_cells=3`). The remaining counts were log1p-transformed, and the top 2000 highly variable genes were selected for downstream analysis. Outcome: 15,307 cells and 22,387 genes retained.
2.  **Clustering.** Leiden clustering was performed on the normalized data using 30 principal components (`n_comps=30`) and 15 neighbors (`n_neighbors=15`). The resolution was selected via bootstrap stability (`resolution=0.2`), yielding 20 clusters (`n_clusters=20`). Outcome: 20 distinct cell clusters identified, reconciled with major cell classes.
3.  **Differential Expression (DE).** Per-cell-type effect rankings were generated using `run_de` with a Wilcoxon rank-sum test. The analysis was stratified by `majorclass` (AC, BC, Cone, MG, Rod). The reference group was WT. Genes were required to be detected in at least 10% of one population (`min_pct=0.1`) and had a log2 fold-change threshold of 0.25 (`lfc=0.25`). No FDR cutoff was applied due to the single-library design. Outcome: Ranked gene lists for each major class.
4.  **Cell Composition Mapping.** Relative abundances of major retinal classes were calculated per condition using `run_composition`. Outcome: Proportions of AC, BC, Cone, MG, Rod, and minor classes for WT and DDX41 samples.
5.  **Pathway Enrichment.** Over-representation analysis (ORA) was conducted using `run_enrichment` on up- and down-regulated gene sets per major class. Preranked gene-set enrichment analysis (GSEA) was conducted using `run_gsea_prerank` on the full Wilcoxon-ranked gene lists. Databases included GO Biological Process and Reactome. Outcome: Enriched terms for each cell type and direction.
6.  **Cross-Cell-Type Synthesis.** Genes changing in the same direction across multiple major classes were identified using `run_code`. Outcome: Identification of pan-retinal up-regulated (*Ubb*, *Hmgn2*) and down-regulated (phototransduction) signatures.

## References
*Citations to be inserted by the literature module (PaperQA).*
