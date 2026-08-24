# Single-cell transcriptomic profiling of the DDX41-mutant retina reveals reactive Müller gliosis, a pan-retinal translation/stress program, and down-regulation of phototransduction

## Abstract

DDX41 is a DEAD-box RNA helicase whose germline mutations predispose to myeloid malignancy, but its role in post-mitotic tissues such as the retina is unknown. We performed a descriptive, exploratory single-cell RNA-seq comparison of one DDX41-mutant retina against one wild-type (WT) retina (15,307 cells total; 9,047 WT and 6,260 DDX41). Because each condition comprised a single biological library, all comparisons are reported as descriptive effect sizes (log2 fold-changes) and rank metrics rather than inferential statistics. Compositionally, Müller Glia (MG) and Amacrine (AC) proportions increased ~2-fold in the DDX41 library (2.13× and 2.20×, respectively) while Bipolar (BC) and Cone proportions decreased (0.68× and 0.32×). Per-cell-type expression ranking across five testable major classes identified a dominant reactive-gliosis signature in MG (*Gfap*, log2FC +6.02), strong up-regulation of translation/ribosomal programs shared across AC, BC, MG and Rod (consensus genes including *Hmgn2*, *Ubb*, *mt-Cytb*, *Rps20*), and down-regulation of phototransduction programs in AC, BC and Rod; cones showed a predominantly up-regulated program with down-regulated glycolysis/hypoxia terms. Pathway over-representation and preranked gene-set enrichment corroborated these patterns. These findings generate the hypothesis that DDX41 loss drives glial expansion and a pan-retinal stress/translation response with photoreceptor programme suppression, and require validation in replicate biological libraries.

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

This dataset asks what changes — at the level of cell-type composition and per-cell-type gene expression — between a DDX41-mutant retina and a WT retina. The question is answerable from this dataset because it contains 15,307 cells spanning 11 annotated retinal major classes with representation of both conditions, permitting a cell-type-resolved descriptive comparison of the mutant transcriptome against WT even though no replicate libraries are present. The analysis is explicitly descriptive: with one biological library per condition, the goal is to generate ranked, effect-size-based hypotheses about the cellular and molecular consequences of the DDX41 mutation, not to claim statistical significance.

## Results

### Quality control

All 15,307 cells passed the pre-defined QC filters (≥200 detected genes, ≤10% mitochondrial reads, singlet status), so no cells were removed; 22,387 genes were retained after filtering to genes detected in ≥3 cells. WT contributed 9,047 cells and DDX41 6,260 cells. Per-cell metrics differed between libraries, with the DDX41 cells showing higher sequencing depth: mean nFeature_RNA was 1,504.6 in WT versus 2,075.8 in DDX41 (median 1,194 vs 1,776.5) and mean nCount_RNA was 2,730.6 versus 4,190.0 (Figure 1). Mitochondrial fractions were low and comparable between conditions (mean 1.60% WT vs 1.50% DDX41). This depth imbalance is relevant to interpretation of the expression results (see Discussion).

![Figure 1. QC violin plots of per-cell quality metrics (genes detected and mitochondrial fraction) for the WT and DDX41 libraries.](figures/violin_qc_violin.png)

### Cell composition

The relative abundance of major classes shifted substantially between the two libraries, although with one library per condition these proportions are descriptive only. The most striking compositional changes were an approximately 2-fold expansion of MG (+6.66 percentage points, pp) and AC (+3.41 pp) in the DDX41 library, and contraction of BC (−6.08 pp) and Cone (−3.73 pp); the Rod proportion was essentially unchanged (Table 1). Microglia showed a 3.62× relative increase, from a small baseline. These shifts are consistent with a reactive, glia-dominated retinal environment in the DDX41 mutant.

| Major class | WT (%) | DDX41 (%) | Fold (DDX41/WT) |
|---|---|---|---|
| Rod | 66.02 | 65.24 | 0.99× |
| BC | 18.88 | 12.80 | 0.68× |
| MG | 5.91 | 12.57 | 2.13× |
| Cone | 5.52 | 1.79 | 0.32× |
| AC | 2.85 | 6.26 | 2.20× |

**Table 1. Cell-type composition by condition.** Descriptive proportions of the five major classes with ≥30 cells in both arms; the remaining six classes (Endothelial, HC, Microglia, Pericyte, RGC, RPE) were each <2% in both libraries.

### Cell clustering

Leiden clustering on the QC-normalized data using the top 30 principal components and 15 neighbors per cell produced 20 clusters at the automatically selected resolution of 0.2 (bootstrap stability 0.9605 ± 0.0081; cluster sizes 15–6,199 cells). Because the dataset already carried `majorclass` and `celltype` annotations, these pre-annotated labels were used for all downstream analyses rather than the anonymous cluster IDs. Reconciliation of Leiden clusters against the pre-annotated labels confirmed that most clusters were dominated by a single major class (e.g., cluster 0: 99.9% Rod, n=6,199; cluster 2: 99.6% MG, n=1,321; cluster 4: 100% Cone, n=607), supporting the quality of the underlying annotations. The UMAP embedding separates the major classes and shows both conditions represented across the trajectory (Figure 2).

![Figure 2. UMAP embedding of all 15,307 cells, coloured by pre-annotated major class and by sample ID (DDX41 vs WT).](figures/umap_majorclass_sampleid.png)

### Per-cell-type differential expression

Five of the 11 major classes had ≥30 cells in both arms and were tested (AC, BC, Cone, MG, Rod); the remaining six classes (Endothelial, HC, Microglia, Pericyte, RGC, RPE) were not covered because of insufficient cells in one or both arms. Within each testable class, a Wilcoxon rank-sum comparison of DDX41 versus WT cells was used strictly as a descriptive ranking (reference = WT, `min_pct=0.1`, |log2FC|≥0.25 descriptive threshold; no FDR was applied, `padj=1.0`), so genes are reported by rank and effect size, not significance (Table 2).

| Class | Genes tested | Up (log2FC≥0.25) | Down (log2FC≤−0.25) | Top up (log2FC) | Top down (log2FC) |
|---|---|---|---|---|---|
| AC | 8,371 | 2,462 | 1,509 | *Angpt1* (+2.58) | *Apoe* (−4.91) |
| BC | 7,309 | 1,810 | 524 | *Xlr3b* (+3.83) | *Sorcs3* (−2.19) |
| Cone | 6,840 | 2,800 | 1,102 | *Cartpt* (+5.99) | *Kcne2* (−2.11) |
| MG | 9,720 | 5,057 | 987 | *Gfap* (+6.02) | *Grin2b* (−3.46) |
| Rod | 4,649 | 3,283 | 132 | *Col25a1* (+8.74) | *Dcc* (−5.83) |

**Table 2. Per-cell-type differential-expression ranking (DDX41 vs WT), descriptive threshold |log2FC| ≥ 0.25.**

The strongest per-class signal was in MG, where the reactive-gliosis marker *Gfap* was the top up-regulated gene (log2FC +6.02), accompanied by immune/complement genes such as *C4b* (+3.26), *Serping1* (+2.90) and *A2m* (+1.70), and by down-regulation of synaptic receptor genes (*Grin2b* −3.46, *Grin2d* −3.18) (Figure 3). Rod cells showed an extreme up-regulation of extracellular-matrix/injury-response genes — *Col25a1* (+8.74), *Fgf2os* (+2.57), *Fgf2* (+2.46) — alongside down-regulation of axon-guidance/synapse genes (*Dcc* −5.83, *Nrn1* −5.35, *Aplp2* −4.91) (Figure 4). Cone cells were overwhelmingly up-regulated (2,800 up vs 1,102 down) with *Cartpt* (+5.99) the strongest signal. AC — a non-photoreceptor interneuron class — showed down-regulation of phototransduction genes (*Apoe* −4.91, *Pde6g* −1.90, *Rho* −1.84, *Gnat1* −1.68) alongside up-regulation of ribosomal/proteasomal genes (*Ubb*, *H3f3b*, Rpl/Rps family). BC showed an up-regulated immune/inflammatory and immediate-early program (*Xlr3b* +3.83, *A2m* +3.32, *Fos* +2.35, *Egr1* +2.48, *Junb* +1.98) with complement components (C4b/Serping1 family).

![Figure 3. Volcano plot of the per-cell-type differential-expression ranking in MG (DDX41 vs WT); genes past the descriptive threshold |log2FC| ≥ 0.25 are highlighted.](figures/volcano_MG.png)

![Figure 4. Volcano plot of the per-cell-type differential-expression ranking in Rod (DDX41 vs WT); genes past the descriptive threshold |log2FC| ≥ 0.25 are highlighted.](figures/volcano_Rod.png)

### Pathway enrichment

Over-representation analysis (ORA) of the top up- and down-regulated genes per class, run against GO Biological Process 2023, Reactome 2022 and MSigDB Hallmark 2020 with the measured retina genes (tested universe n=11,293) as background, echoed the per-gene patterns. Up-regulated genes in AC, BC, MG and Rod were dominated by translation-related terms (Cytoplasmic Translation GO:0002181; Eukaryotic Translation Elongation R-HSA-156842; Peptide Chain Elongation R-HSA-156902; Formation of a Pool of Free 40S Subunits R-HSA-72689; Nonsense-Mediated Decay Independent of EJC R-HSA-975956). Down-regulated genes in AC, BC and Rod were dominated by phototransduction terms (Phototransduction Cascade R-HSA-2514856; Visual Phototransduction R-HSA-2187338; Sensory Perception of Light Stimulus GO:0050953), and in Rod also by glycolysis/hypoxia terms. Cones were the exception: their down-regulated genes enriched for Glycolytic Process (GO:0006096, adjusted p = 2.6e-10), Carbohydrate Catabolic Process (1.1e-09), Pyruvate Metabolic Process (6.6e-09), Hypoxia (1.4e-08) and mTORC1 Signaling (7.2e-07), while up-regulated genes trended toward Oxidative Phosphorylation and translation terms (Figure 5).

![Figure 5. Top over-represented pathway terms for up-regulated genes in Rod (DDX41 vs WT) against Reactome 2022 gene sets.](figures/enrichment_Rod_up.png)

Preranked GSEA of the complete per-class Wilcoxon rankings (min_size=15, max_size=500, 1,000 permutations, seed 42, FDR<0.05) returned signed enrichment scores consistent with ORA: AC 309 significant gene sets (285 up, 24 down), BC 58 (51 up, 7 down), Cone 25 (1 up, 24 down), MG 308 (248 up, 60 down), Rod 34 (24 up, 10 down). The strongest up-regulated programs were translation elongation in AC (NES 3.11) and MG (NES 3.48), and the strongest down-regulated programs were phototransduction in Rod (Visual Phototransduction R-HSA-2187338, NES −4.25; Phototransduction Cascade R-HSA-2514856, NES −4.12) and BC, and glycolysis/hypoxia in Cone (Pyruvate Metabolic Process NES −3.05; Hypoxia NES −2.59).

### Cross-cell-type synthesis

Aggregating the per-class rankings (|log2FC|≥0.25), 1,715 genes were up-regulated in the consensus ranking across the five testable classes. The top shared up-regulated genes — *Hmgn2* (max log2FC 1.61, up in 5/5 classes, consensus 0.664), *Ubb* (max 1.05, 5/5, consensus 0.66), *mt-Cytb* (max 2.07, 4/5, consensus 0.575), *Rps20* (max 1.32, 5/5, consensus 0.486) and *Klhl29* (max 3.24, 4/5) — point to a pan-retinal up-regulation of ribosomal/translation-related genes in the DDX41 mutant, a signal that is directionally uniform across cell types and therefore potentially confounded by the higher sequencing depth of the DDX41 library.

## Discussion

The DDX41-mutant retina shows a coordinated, cell-type-resolved pattern of change that is best read as a set of hypotheses rather than definitive biology. Four features stand out. First, the ~2-fold expansion of MG and AC with corresponding contraction of BC and Cone points to a compositional reorganization of the inner retina, and the massive up-regulation of *Gfap* (log2FC +6.02) — the strongest single-gene signal in any class — together with immune/complement genes (*C4b*, *Serping1*, *A2m*) describes a classic reactive Müller gliosis response. Second, a pan-retinal translation/ribosomal program (translation-elongation terms, *Hmgn2*, *Ubb*, *Rps20*, *mt-Cytb*) was up-regulated across AC, BC, MG and Rod, which we interpret cautiously: it is exactly this kind of same-direction, housekeeping-gene shift that a between-library depth difference can produce, and the DDX41 library does have higher per-cell depth (mean nFeature_RNA 2,075.8 vs 1,504.6). Third, phototransduction programs were down-regulated in AC, BC and Rod, suggesting suppression of photoreceptor-signalling machinery in the mutant; notably, the Rod fraction itself was unchanged despite large transcriptional change. Fourth, cones behaved distinctly — a predominantly up-regulated program (Oxidative Phosphorylation, translation) with down-regulated glycolysis/hypoxia terms — which we interpret as a survival/compensatory response rather than simple degeneration. DDX41 is a DEAD-box RNA helicase implicated in RNA processing and innate immune signalling, and its germline mutations are best known for predisposition to myeloid malignancy; these retinal findings extend the potential cellular consequences of DDX41 dysfunction to a post-mitotic, non-hematopoietic tissue, where loss appears to drive glial activation and a generic cellular stress/translation response. All of these observations are descriptive and require validation.

## Limitations

- **Single biological library per condition (one donor each); no replicates.** Cells within a class are subsamples of one donor (pseudoreplication), so Wilcoxon scores, p-values and enrichment statistics are descriptive rank metrics, not inferential differential expression; no adjusted p-values or FDR cutoffs were applied (`padj=1.0`).
- **Between-library depth imbalance.** The DDX41 library had higher mean nFeature_RNA and nCount_RNA than WT; the pan-retinal up-regulation of translation/ribosomal genes is directionally uniform across cell types and may be partly depth-confounded rather than biological.
- **6 of 11 major classes not covered** (Endothelial, HC, Microglia, Pericyte, RGC, RPE) because they had <30 cells in one or both arms; no findings are reported for them.
- **Composition shifts are descriptive only** — with one library per condition, proportion differences cannot be tested statistically.
- **ORA and preranked GSEA test different inputs under different null hypotheses** and are not expected to agree; enrichment reflects association with an expression program, not evidence of pathway activity or causation.
- **No genome assembly** was reported in the dataset metadata; no reference-based (foundation-model) annotation step was run — the dataset's own pre-annotated `majorclass`/`celltype` labels were used throughout.

## Conclusion

This study performed a descriptive single-cell RNA-seq comparison of a DDX41-mutant retina against a WT retina, profiling 15,307 cells across 11 annotated major classes. The principal finding is a coordinated pattern of reactive Müller gliosis (*Gfap* up-regulation, MG expansion), a pan-retinal translation/stress program, down-regulation of phototransduction in AC, BC and Rod, and a distinct compensatory cone signature. The next step is validation in replicate biological libraries with matched sequencing depth, followed by independent experimental confirmation (e.g., immunohistochemistry for GFAP and assessment of ribosomal/translational activity) of the reactive-gliosis and stress hypotheses.

## Methods

1. **Dataset.** A pre-processed single-cell RNA-seq AnnData object (`Ddx41_DEG.h5ad`) containing 15,307 cells and 33,696 genes. The `sampleid` column carried the DDX41 vs WT contrast (WT: 9,047 cells; DDX41: 6,260 cells); `orig.ident` was at a single level, confirming exactly one biological library per condition and no replicates. No genome assembly was reported in the dataset metadata.

2. **Quality control.** Cells were filtered using existing metadata columns with the thresholds: ≥200 detected genes, ≤10% mitochondrial reads, and singlet status (`DF.classifications`). All 15,307 cells passed these filters (0% removed); 22,387 genes were retained after filtering to genes detected in ≥3 cells.

3. **Normalization and gene selection.** Counts were normalized and log1p-transformed; the top 2,000 highly variable genes were selected for downstream structural analyses.

4. **Dimensionality reduction.** Principal component analysis was run on the 2,000 HVGs; the top 30 principal components were retained.

5. **Neighbor graph construction.** A shared-nearest-neighbour graph was built with 15 neighbors per cell.

6. **Clustering.** Leiden clustering was run on the QC-normalized data using the top 30 PCs and 15 neighbors. The resolution was selected automatically by bootstrap stability (n_bootstrap=10, stability_min=0.9), yielding resolution 0.2 with 20 clusters (stability 0.9605 ± 0.0081; cluster sizes 15–6,199 cells). Because the dataset already carried `majorclass` and `celltype` annotations, these pre-annotated labels were used for all downstream analyses rather than the anonymous cluster IDs.

7. **UMAP embedding.** A UMAP embedding of all cells was computed for visualization of clusters and annotated classes; both conditions were represented across the embedding.

8. **Cell-type annotation.** No reference-based or foundation-model annotation (e.g., scGPT) was run; the dataset's pre-existing `majorclass` and `celltype` annotations were used throughout, and Leiden clusters were reconciled against them for quality assessment (e.g., cluster 0: 99.9% Rod; cluster 2: 99.6% MG; cluster 4: 100% Cone).

9. **Cell composition.** The fraction of cells belonging to each major class was computed per condition. With one library per condition, proportions are reported descriptively without statistical testing.

10. **Per-cell-type differential expression ranking.** Within each of the 11 major classes, a Wilcoxon rank-sum test compared every DDX41 cell against every WT cell in the same class (reference = WT). Genes were required to be detected in ≥10% of one population (`min_pct=0.1`), and a log2 fold-change threshold of 0.25 was applied. Because cells are subsamples of a single donor per condition (pseudoreplication), no adjusted p-values or FDR cutoffs were applied (`padj=1.0`); Wilcoxon scores are reported strictly as non-inferential rank metrics and log2FC values are the primary descriptive signal. Only 5 of 11 major classes had ≥30 cells in both arms and were tested (AC, BC, Cone, MG, Rod); the remaining 6 classes (Endothelial, HC, Microglia, Pericyte, RGC, RPE) were not covered because they had fewer than 30 cells in one or both arms (Endothelial: condition=7, reference=27; HC: condition=5, reference=11; Microglia: condition=60, reference=24; Pericyte: condition=4, reference=2; RGC: condition=6, reference=8; RPE: condition=2, reference=2). The top 50 genes per direction per class were reported.

11. **Pathway over-representation (ORA).** Top up- and down-regulated genes per major class (|log2FC|≥0.25) were tested for over-representation against local pathway libraries (GO Biological Process 2023, Reactome 2022, MSigDB Hallmark 2020) using the genes actually measured in this retina (tested universe, n=11,293) as background. The top 10 enriched terms per direction per class were reported.

12. **Preranked gene-set enrichment (GSEA).** The complete Wilcoxon-ranked gene lists per major class were walked against the same three pathway libraries (min_size=15, max_size=500, 1,000 permutations, seed 42, FDR<0.05), returning signed normalized enrichment scores (NES) and FDR q-values.

13. **Cross-cell-type synthesis.** Per-class DE tables were aggregated and filtered to genes with |log2FC|≥0.25; a consensus ranking metric (abs(log2FC) × fraction of classes expressing the gene) was computed to prioritize robust, shared changes across cell types.

## References

none retrieved
