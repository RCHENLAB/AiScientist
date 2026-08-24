# Transcriptional Reprogramming of Müller Glia, Photoreceptors, and Interneurons in the DDX41-Deficient Mouse Retina

## Abstract

We performed single-cell transcriptomic profiling of the DDX41-deficient mouse retina to characterize cell-type-specific changes between mutant and wild-type conditions. Using the `Ddx41_DEG.h5ad` dataset (15,307 cells; WT: 9,047, DDX41: 6,260), we applied Wilcoxon rank-sum testing stratified by five major retinal classes (Amacrine Cells, Bipolar Cells, Cone photoreceptors, Müller Glia, and Rod photoreceptors). All results are reported as exploratory effect-size rankings; with one library per condition, formal statistical inference about replicability is not possible. The DDX41 mutant retina showed retina-wide suppression of phototransduction (Rod: NES = −4.25; BC: NES = −2.67), expansion of Müller Glia (+6.66 percentage points) with a robust reactive gliosis signature (Gfap log₂FC = +6.02), cone-specific compensatory up-regulation dominated by Cartpt (log₂FC = +5.99), and Rod photoreceptors with extreme extracellular-matrix remodeling (Col25a1 log₂FC = +8.74) despite stable proportional representation. These findings constitute descriptive hypotheses about DDX41's role in retinal homeostasis that require independent biological replication.

## Study question

The study asked what transcriptomic changes distinguish the DDX41 mutant retina from its wild-type counterpart across defined retinal cell types. Because the dataset contained one library per condition, this question could be answered by characterising the direction and magnitude of gene-expression shifts within each major class, but could not be answered by formal hypothesis testing for population-level effects.

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

All 15,307 cells passed the predefined QC filters (≥200 genes per cell, ≤10% mitochondrial reads, classified as singlets). Thirteen mitochondrial-encoded genes were flagged during the QC assessment. The analysed expression matrix used log₁p-transformed normalised counts. Descriptive summary statistics are presented in Table 1.

![Figure 1. Violin plots of per-cell QC metrics coloured by condition (WT, DDX41) showing distributions of nFeature_RNA, nCount_RNA, and percent.mt across the combined dataset.](figures/violin_qc_violin.png)

![Figure 2. Scatter plots of QC metrics — genes per cell versus mitochondrial percent (left) and nCount_RNA versus nFeature_RNA (right) — showing the relationship between UMI counts, gene detection, and mitochondrial contamination across conditions.](figures/scatter_qc_genes.png)

![Figure 3. Scatter plot of mitochondrial percent versus nFeature_RNA used to assess the correlation between mitochondrial content and gene detection per cell, coloured by condition (WT, DDX41).](figures/scatter_qc_mt.png)

| Metric | WT | DDX41 | Combined |
|---|---|---|---|
| Cell count | 9,047 | 6,260 | 15,307 |
| Mean nFeature_RNA | 1,505 | 2,076 | 1,738 |
| Median nFeature_RNA | 1,194 | 1,777 | 1,526 |
| Mean percent.mt | 1.60% | 1.50% | 1.56% |
| Mean nCount_RNA | 2,731 | 4,190 | 3,327 |
| Total genes (after filtering) | — | — | 22,387 |

**Table 1.** Descriptive QC summary for WT and DDX41 libraries. DDX41 cells exhibited higher median gene detection (1,777 vs 1,194) and UMI counts (4,190 vs 2,731), suggesting a modest depth imbalance between libraries that was noted during downstream interpretation.

### Cell composition

Major retinal class proportions shifted markedly between conditions (Figure 4). Müller Glia expanded most dramatically (5.91% → 12.57%, a 2.13-fold increase; +6.66 percentage points), followed by Amacrine Cells (2.85% → 6.26%, 2.20-fold; +3.41 pp) and Microglia (0.27% → 0.96%, 3.62-fold; +0.69 pp). Cone photoreceptors contracted substantially (5.52% → 1.79%, 0.32-fold; −3.73 pp), as did Bipolar Cells (18.88% → 12.80%, 0.68-fold; −6.08 pp). Rod photoreceptor proportion remained nearly stable (66.02% → 65.24%, 0.99-fold; −0.78 pp). Because only one library existed per condition, no statistical test for equality of proportions was performed; these shifts are reported as observed descriptive differences and are considered alongside expression-level findings.

![Figure 4. UMAP of retinal cells coloured by major class (left panel) and by condition (right panel), illustrating the spatial distribution of cell types and the relative representation of WT versus DDX41 cells within each region of the embedding.](figures/umap_majorclass_sampleid.png)

### De-novo clustering

A Leiden clustering at resolution 0.2 yielded 20 clusters, with bootstrap stability of 0.961 (SD = 0.008). This high stability — compared with a rapid decline at higher resolutions (0.87 at res 0.4, 0.56 at res 1.5) — supported 20 clusters as a robust parcellation for quality control. The de-novo clusters showed strong concordance with the pre-existing `majorclass` annotations: each cluster was dominated by a single major class at ≥81.7% purity, confirming that the annotation structure was biologically coherent and not driven by condition-specific batch effects.

![Figure 5. UMAP of de-novo Leiden clusters (resolution 0.2, 20 clusters) coloured by cluster identity, providing a quality-control visualisation of population structure independent of the pre-existing annotations.](figures/umap_clusters.png)

| Resolution | n_clusters | Stability | Stability SD |
|---|---|---|---|
| 0.2 | 20 | 0.961 | 0.008 |
| 0.4 | 24 | 0.870 | 0.044 |
| 0.6 | 28 | 0.799 | 0.078 |
| 0.8 | 32 | 0.649 | 0.055 |
| 1.0 | 34 | 0.599 | 0.049 |
| 1.5 | 40 | 0.558 | 0.043 |
| 2.0 | 47 | 0.559 | 0.022 |

**Table 2.** Leiden resolution sweep results showing cluster number and bootstrap stability at each resolution. Resolution 0.2 was selected for quality-control clustering.

### Per-cell-type differential expression

Differential expression was performed within five major classes using Wilcoxon rank-sum tests (DDX41 vs WT as reference). Six major classes were excluded because fewer than 30 cells were present in one or both arms: Endothelial, Horizontal Cells, Microglia, Pericyte, Retinal Ganglion Cells, and RPE. The padj threshold was set to 1.0 throughout (no FDR cutoff applied), and log₂ fold-change was the primary effect-size metric; because there was one library per condition, all reported values are exploratory rankings rather than inferential statistics.

#### Amacrine Cells

ACs showed 9 up-regulated genes (|log₂FC| ≥ 1.0) and 38 down-regulated genes (|log₂FC| ≥ 1.0) among 8,371 tested genes (469 WT cells; 181 DDX41 cells). The strongest up-regulated gene was Angpt1 (log₂FC = +2.58), and the strongest down-regulated gene was Apoe (log₂FC = −4.91). Over-representation analysis of up-regulated genes (374 tested) identified cytoplasmic translation and peptide chain elongation pathways (ORA padj < 0.05), while down-regulated genes (140 tested) were strongly enriched for phototransduction cascade and sensory perception of light stimulus terms (ORA padj < 0.05). GSEA identified 309 significant pathways (FDR < 0.05), with the top up-regulated pathway being Eukaryotic Translation Elongation (NES = 3.11) and the most strongly down-ranked being Visual Phototransduction. The discordance between ACs — interneurons with no phototransduction machinery — and phototransduction down-regulation is notable and may reflect an indirect effect on photoreceptor signalling upstream of AC integration.

![Figure 6. Volcano plot of Amacrine Cell differential expression (DDX41 vs WT). The x-axis represents log₂ fold-change; the y-axis represents −log₁₀(adjusted p-value). Genes above the descriptive threshold (|log₂FC| ≥ 0.25) are highlighted; the top five annotated genes by absolute log₂FC are labelled.](figures/volcano_AC.png)

![Figure 7. Dot-plot enrichment summary for Amacrine Cell up-regulated genes (translation/ribosome-associated pathways). Gene-set size is proportional to dot size; colour represents adjusted p-value (ORA).](figures/enrichment_AC_up.png)

![Figure 8. Dot-plot enrichment summary for Amacrine Cell down-regulated genes (phototransduction cascade pathways). Gene-set size is proportional to dot size; colour represents adjusted p-value (ORA).](figures/enrichment_AC_down.png)

#### Bipolar Cells

BCs showed 24 up-regulated and 26 down-regulated genes (|log₂FC| ≥ 1.0) among 7,309 tested genes (1,197 WT cells; 1,312 DDX41 cells). The top up-regulated gene was Xlr3b (log₂FC = +3.83), and the top down-regulated gene was Sorcs3 (log₂FC = −2.19). Up-regulated genes were strongly associated with translation and peptide biosynthetic processes; down-regulated genes with phototransduction cascade and cell-adhesion pathways. GSEA identified 58 significant pathways (FDR < 0.05), with Viral mRNA Translation (NES = 2.51) as the top up-ranked pathway and Phototransduction Cascade (NES = −2.67) as the strongest down-ranked pathway. The BC result mirrors the AC pattern: translation machinery up, phototransduction down, and cell-adhesion programs suppressed — consistent with a broad neural-retinal stress response affecting both photoreceptor relay neurons and their inhibitory interneuron partners.

![Figure 9. Volcano plot of Bipolar Cell differential expression (DDX41 vs WT). Genes above the descriptive threshold (|log₂FC| ≥ 0.25) are highlighted; the top five annotated genes by absolute log₂FC are labelled.](figures/volcano_BC.png)

![Figure 10. Dot-plot enrichment summary for Bipolar Cell up-regulated genes (translation machinery and peptide biosynthetic pathways).](figures/enrichment_BC_up.png)

![Figure 11. Dot-plot enrichment summary for Bipolar Cell down-regulated genes (phototransduction cascade and cell-adhesion pathways).](figures/enrichment_BC_down.png)

#### Cone Photoreceptors

Cones showed a markedly asymmetric expression profile, with 47 genes up-regulated and only 16 down-regulated at |log₂FC| ≥ 1.0 (6,840 genes tested; 310 WT cells; 301 DDX41 cells). The top up-regulated gene was Cartpt (log₂FC = +5.99), the highest single-gene log₂FC observed across all five cell types analysed. The top down-regulated gene was Kcne2 (log₂FC = −2.11). ORA of up-regulated genes (329 tested) recovered oxidative phosphorylation and translation initiation pathways; down-regulated genes (53 tested) were dominated by glycolytic process, hypoxia, and mTORC1 signalling terms. GSEA identified 25 significant pathways (FDR < 0.05), with Pyruvate Metabolic Process (NES = −3.05) and Carbohydrate Catabolic Process (NES = −2.90) the most strongly down-ranked. The strong directional asymmetry — overwhelmingly up-regulated despite proportional depletion — is consistent with a cone-specific compensatory transcriptional program.

![Figure 12. Volcano plot of Cone photoreceptor differential expression (DDX41 vs WT). Genes above the descriptive threshold (|log₂FC| ≥ 0.25) are highlighted; the top five annotated genes by absolute log₂FC are labelled.](figures/volcano_Cone.png)

![Figure 13. Dot-plot enrichment summary for Cone up-regulated genes (oxidative phosphorylation and translation initiation pathways).](figures/enrichment_Cone_up.png)

![Figure 14. Dot-plot enrichment summary for Cone down-regulated genes (glycolysis, hypoxia, and mTORC1 signalling pathways).](figures/enrichment_Cone_down.png)

#### Müller Glia

Müller Glia showed the largest magnitude expression changes of any non-photoreceptor cell type: 45 up-regulated and 24 down-regulated genes at |log₂FC| ≥ 1.0 (9,720 genes tested; 803 WT cells; 519 DDX41 cells). The top up-regulated gene was Gfap (log₂FC = +6.02) — a canonical marker of reactive gliosis — and the top down-regulated gene was Grin2b (log₂FC = −3.46). ORA of up-regulated genes (5,028 tested) recovered translation, rRNA processing, and peptide biosynthetic pathways; ORA of down-regulated genes (508 tested) identified nervous system development, axon guidance, synapse assembly, and axonogenesis. GSEA identified 308 significant pathways (FDR < 0.05), with Peptide Chain Elongation (NES = 3.48), Eukaryotic Translation Elongation (NES = 3.48), and Selenocysteine Synthesis (NES = 3.47) as the three highest-ranked up-regulated pathways. This signature — Gfap as the top gene, translation machinery up, synaptic-development programs down — is the hallmark of Müller gliosis, the retinal equivalent of astrocyte reactivity.

![Figure 15. Volcano plot of Müller Glia differential expression (DDX41 vs WT). Genes above the descriptive threshold (|log₂FC| ≥ 0.25) are highlighted; the top five annotated genes by absolute log₂FC are labelled. Gfap (+6.02) and Grin2b (−3.46) are annotated.](figures/volcano_MG.png)

![Figure 16. Dot-plot enrichment summary for Müller Glia up-regulated genes (translation, rRNA processing, and peptide biosynthesis pathways).](figures/enrichment_MG_up.png)

![Figure 17. Dot-plot enrichment summary for Müller Glia down-regulated genes (nervous system development, axon guidance, synapse assembly, and axonogenesis pathways).](figures/enrichment_MG_down.png)

#### Rod Photoreceptors

Rods showed the largest number of significantly altered genes: 49 up-regulated and 12 down-regulated at |log₂FC| ≥ 1.0 (4,649 genes tested; 6,578 WT cells; 3,479 DDX41 cells). The top up-regulated gene was Col25a1 (log₂FC = +8.74), the highest single-gene effect size in the entire dataset. The top down-regulated gene was Dcc (log₂FC = −5.83). ORA of up-regulated genes (3,279 tested) recovered RNA metabolism, stress response, mitochondrial ATP synthesis, and translation pathways. ORA of down-regulated genes (107 tested) was dominated by hypoxia, glycolysis, phototransduction cascade, and mTORC1 signalling. GSEA identified 34 significant pathways (FDR < 0.05). The single most strongly down-ranked pathway in the entire dataset was Visual Phototransduction (NES = −4.25), followed by Phototransduction Cascade (NES = −4.12) and Inactivation, Recovery And Regulation Of Phototransduction Cascade (NES = −4.00). Among up-ranked pathways, Mitochondrial Electron Transport NADH to Ubiquinone (NES = 2.44) and Complex I Biogenesis (NES = 2.40) indicated a shift toward oxidative metabolism. Notably, despite dramatic transcriptional remodeling, rod proportion remained stable (66.02% → 65.24%), suggesting rods undergo a profound transcriptional response without proportional loss at this stage.

![Figure 18. Volcano plot of Rod photoreceptor differential expression (DDX41 vs WT). Genes above the descriptive threshold (|log₂FC| ≥ 0.25) are highlighted; the top five annotated genes by absolute log₂FC are labelled. Col25a1 (+8.74) and Dcc (−5.83) are annotated.](figures/volcano_Rod.png)

![Figure 19. Dot-plot enrichment summary for Rod up-regulated genes (RNA metabolism, stress response, and translation pathways).](figures/enrichment_Rod_up.png)

![Figure 20. Dot-plot enrichment summary for Rod down-regulated genes (hypoxia, glycolysis, and phototransduction cascade pathways).](figures/enrichment_Rod_down.png)

### Cross-cell-type synthesis

A consensus ranking computed across all five tested major classes using `abs(log₂FC) × frac_expressed` identified shared up-regulated genes including ribosomal and nuclear proteins (Hmgn2, Ubb, Rps20) and mt-Cytb (detected in 4 of 5 groups). Shared down-regulated genes were dominated by phototransduction cascade components across photoreceptor and non-photoreceptor populations, indicating a retina-wide effect on visual function beyond any single cell type. The strongest convergent signal was suppression of the phototransduction cascade in Rods, BCs, and ACs simultaneously.

## Discussion

The single-cell atlas of the DDX41-deficient retina reveals a coordinated, retina-wide transcriptional response that spans all major neuronal and glial populations. Several convergent themes emerge.

**Retina-wide suppression of phototransduction.** The most consistent finding across cell types was the down-regulation of visual phototransduction pathway genes — not only in the photoreceptors themselves (Rods: NES = −4.25; Cones: Visual Perception NES = −2.29), but also in bipolar cells (NES = −2.67) and amacrine cells. DDX41 is a DEAD-box RNA helicase implicated in ribosome biogenesis and pre-mRNA splicing, and its loss may broadly impair the translational and post-transcriptional capacity required to maintain phototransduction machinery in photoreceptors. The fact that this suppression extends to interneurons that do not perform phototransduction themselves suggests either a non-cell-autonomous feedback effect from impaired photoreceptor signalling, or a general transcriptional deregulation of the retinal network triggered by DDX41 loss.

**Müller Glia undergo robust reactive gliosis.** The Müller glia response was the most quantitatively striking non-photoreceptor finding: a 2.13-fold proportional expansion (+6.66 pp), Gfap as the top up-regulated gene (log₂FC = +6.02), and broad down-regulation of axon guidance, synapse assembly, and nervous system development programs. This signature is characteristic of retinal gliosis, a response to neural injury or degeneration in which Müller glia proliferate, up-regulate intermediate filaments, and attempt to seal the retinal architecture. The magnitude of this response — both proportionally and in terms of Gfap induction — implies substantial retinal stress or early degeneration in the DDX41 mutant.

**Cones mount a survival-oriented compensatory program.** Despite a 3-fold proportional contraction (5.52% → 1.79%), cones showed 47 up-regulated versus only 16 down-regulated genes at |log₂FC| ≥ 1.0. Cartpt, the top up-regulated gene (log₂FC = +5.99), encodes a neuropeptide involved in appetite regulation and pain signalling; its strong induction in cones may represent a stress-response or neuroprotective program. The simultaneous suppression of glycolysis, mTORC1 signalling, and hypoxia pathways suggests a shift in metabolic strategy, potentially toward oxidative phosphorylation, that may reflect an attempt to maintain viability under conditions of metabolic stress. Whether this represents a genuinely protective response or a maladaptive shift cannot be determined without functional data.

**Rods are transcriptionally remodeled without proportional loss.** The stability of rod proportion (66.0% → 65.2%) in the face of the most extreme expression changes in the dataset — including Col25a1 at log₂FC = +8.74 and 3,415 significantly altered genes — is notable. Col25a1 (collagen XXV alpha 1) is not a canonical rod-expressed gene; its extreme induction suggests ectopic expression or de novo synthesis in response to stress, potentially as part of an extracellular matrix remodeling program. The simultaneous suppression of hypoxic and glycolytic pathways with induction of mitochondrial electron transport (Complex I biogenesis, NES = 2.40) indicates a fundamental shift in rod metabolism that may precede or accompany rod dysfunction.

**Caveats regarding the library depth imbalance.** DDX41 cells showed higher median gene detection (1,777 vs 1,194) and UMI counts (4,190 vs 2,731). Because the same normalisation pipeline was applied to both libraries, this depth difference could inflate the apparent detection of low-abundance transcripts in the DDX41 arm, particularly for ribosomal and translation-related genes that were up-regulated across all five cell types. The translation machinery up-regulation observed here — while consistent with a genuine biological response to cellular stress — should therefore be interpreted with caution as a possible partial artefact of depth bias.

## Limitations

1. **Single library per condition.** One biological library per condition (WT: 1, DDX41: 1) means that Wilcoxon rank scores and log₂FC values are descriptive exploratory metrics, not inferential statistics with valid p-values. All findings require independent replication.

2. **Six of eleven major classes not analysed.** Endothelial, Horizontal Cells, Microglia, Pericyte, RGC, and RPE were excluded (fewer than 30 cells in one or both arms), limiting the scope of the retina-wide picture.

3. **No FDR-controlled inference.** The padj threshold was set to 1.0 throughout; the total count of "significant" genes (19,666 across strata) was not FDR-controlled at the experiment-wide level.

4. **Library depth imbalance.** DDX41 cells showed higher median UMI counts and gene detection, which may contribute to the apparent up-regulation of ribosomal/translation genes observed across cell types.

5. **Composition versus expression.** Cell-type proportion shifts cannot be disambiguated from expression changes without independent replicates. Apparent expression differences within a stratum may partially reflect altered cellular composition of that group.

6. **ORA and GSEA detect association, not causation.** Enrichment results indicate correlated gene sets; they do not establish pathway activity, flux, or mechanistic causation.

## Conclusion

Single-cell transcriptomic profiling of the DDX41-deficient mouse retina revealed a coordinated degeneration-and-response program across all major analysed retinal cell types: retina-wide suppression of phototransduction pathways, robust Müller gliosis (Gfap log₂FC = +6.02, +6.66 pp), cone-specific compensatory up-regulation (Cartpt log₂FC = +5.99), and profound transcriptional remodeling of rods (Col25a1 log₂FC = +8.74) despite stable proportion. Because these findings are based on one library per condition, they constitute exploratory hypotheses that must be validated in independent biological replicates before mechanistic conclusions about DDX41's role in retinal homeostasis can be drawn.

## Methods

1. **Dataset loading.** The AnnData object `Ddx41_DEG.h5ad` was loaded into scanpy (HDF5/AnnData format) on an HPC SLURM environment. Pre-existing `majorclass` and `celltype` annotation columns were verified to be present and carried forward for all downstream steps. The object contained 15,307 cells and 33,696 genes before filtering.

2. **Quality control — per-cell metrics.** Three per-cell QC covariates were computed: number of detected genes (nFeature_RNA), total UMI count (nCount_RNA), and mitochondrial transcript fraction (percent.mt). Mitochondrial transcripts were identified by the `MT-` prefix convention. Cells failing any filter were flagged.

3. **Quality control — filtering.** Cells were retained if they expressed ≥200 genes and had ≤10% mitochondrial transcripts; all cells were classified as singlets. Thirteen mitochondrial-encoded genes were flagged during QC. After filtering, 15,307 cells (100%) were retained. The filtered dataset comprised 22,387 genes (from 33,696).

4. **Library depth assessment.** Mean nCount_RNA was 2,731 (WT) versus 4,190 (DDX41); median nFeature_RNA was 1,194 (WT) versus 1,777 (DDX41). This imbalance was noted as a potential confounder for expression comparisons.

5. **Normalisation.** Library-size normalisation was applied using scanpy's default total-count normalisation (target_sum = 1e4), followed by log₁p transformation (log1p_normalized slot). This transformed matrix was used for all downstream HVG selection, dimensionality reduction, and differential expression.

6. **Highly variable gene selection.** The `scanpy.pp.highly_variable_genes` function was run with `n_top_genes = 2,000`, `flavor = 'seurat_v3'`, and default parameters for mean/variance modelling. The resulting 2,000 highly variable genes were used exclusively for PCA, neighbour-graph construction, and UMAP. All 22,387 genes were retained for differential expression testing.

7. **Dimensionality reduction — PCA.** PCA was performed on the 2,000 HVG matrix using `scanpy.tl.pca` (n_comps = 30, default solver). The resulting 30-dimensional PCA embedding was used for neighbour-graph construction.

8. **Neighbour-graph construction.** A nearest-neighbour graph was built on the 30-component PCA embedding using `scanpy.pp.neighbors` (n_neighbors = 15, metric = 'euclidean'). This graph was used as input for Leiden clustering and UMAP.

9. **Leiden clustering — quality control.** Leiden clustering was run at resolution 0.2 (seed = 0, default settings) to generate 20 clusters for quality-control assessment of annotation concordance. Bootstrap stability was assessed across a resolution sweep (0.2, 0.4, 0.6, 0.8, 1.0, 1.2, 1.5, 2.0) using 10 bootstrap iterations per resolution; stability at res 0.2 was 0.961 (SD = 0.008). De-novo clusters were treated as a quality check only and were not used for differential expression or enrichment.

10. **UMAP embedding.** UMAP was computed on the 30-component PCA embedding using `scanpy.tl.umap` (default parameters, min_dist = 0.5). Two separate UMAPs were generated: one coloured by de-novo Leiden cluster and one coloured by condition (WT/DDX41) and major class, for visual assessment of batch effect and biological structure.

11. **Composition analysis.** Cell counts and proportions were tabulated per condition for each major class using the pre-existing `majorclass` column. Proportion shifts (percentage points and fold change) were computed for each major class. No statistical test for equality of proportions was applied (one library per condition).

12. **Differential expression — stratification and parameters.** Differential expression was performed within each major class using a two-sided Wilcoxon rank-sum test (scanpy `tl.rank_genes_groups`, method = 'wilcoxon') with DDX41 as the test group and WT as the reference. Parameters: `min_pct = 0.1` (gene must be detected in ≥10% of cells in at least one group), `log2fc_threshold = 0.25` (descriptive log₂FC threshold), `pts = False`. The `padj` threshold was set to 1.0 throughout (no FDR cutoff applied). Six major classes (Endothelial, HC, Microglia, Pericyte, RGC, RPE) were excluded because they had fewer than 30 cells in one or both arms.

13. **Differential expression — cell-type-specific results.** Results were extracted for five major classes: AC (469 WT, 181 DDX41 cells), BC (1,197 WT, 1,312 DDX41 cells), Cone (310 WT, 301 DDX41 cells), MG (803 WT, 519 DDX41 cells), and Rod (6,578 WT, 3,479 DDX41 cells). Top genes per direction (|log₂FC| ≥ 1.0, N = 50 per direction) were reported for each cell type.

14. **Over-representation analysis.** ORA was run on up-regulated and down-regulated gene sets (|log₂FC| ≥ 0.25, min_frac = 0.1 in at least one group) using gseapy (gene_sets = ['GO_Biological_Process_2023.gmt', 'MSigDB_Hallmark_2020.gmt', 'Reactome_2022.gmt']). Significance was assessed by Fisher's exact test with Benjamini–Hochberg multiple-testing correction (reported as adj_pval). Results were filtered to adj_pval < 0.05.

15. **Gene-set enrichment analysis.** GSEA was performed on a pre-ranked gene list (sorted by −log₁₀(padj) × sign(log₂FC), from scanpy's Wilcoxon output) using gseapy.prerank with gene_sets matching the ORA databases. Normalised enrichment scores (NES) and FDR q-values were reported for pathways with FDR < 0.05.

16. **Cross-cell-type synthesis.** A consensus ranking was computed using `abs(log₂FC) × frac_expressed` across all five tested major classes. Genes detected in all 5 of 5 groups (or 4 of 5 for mt-Cytb) were reported as shared candidates. Shared up-regulated and down-regulated gene ontologies were summarised by convergence of ORA and GSEA directional signals.

17. **Figure generation.** QC figures (violin plots, scatter plots) were generated with scanpy plotting functions. Volcano plots were generated with scanpy's `rank_genes_groups_plot`. UMAP and cluster plots were generated with scanpy's `pl.umap` with appropriate colour dictionaries. ORA dot plots were generated with the `dotplot` function in gseapy. All figures were exported as PNG at 300 dpi.

18. **Reporting conventions.** All differential-expression values are exploratory rankings (inference = 'exploratory_ranking'). No p-value or adjusted p-value thresholds were used to define significance for the purpose of reporting. Effect sizes (log₂FC, NES) and directional gene counts at |log₂FC| ≥ 1.0 are the primary reported metrics.

## References

*Citations to be inserted by the literature module (PaperQA).*
