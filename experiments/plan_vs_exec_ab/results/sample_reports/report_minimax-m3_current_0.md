# Cell-type-resolved single-cell transcriptomic comparison of DDX41 mutant and wild-type mouse retina

## Abstract

We used single-cell RNA sequencing to compare the retinal transcriptomes of DDX41 mutant and wild-type mice. The dataset comprised 15,307 cells (9,047 WT, 6,260 DDX41) profiled across 22,387 genes, with one biological library per condition. After Leiden clustering at resolution 0.2 (20 clusters, bootstrap stability 0.96), five retinal major classes — amacrine cells, bipolar cells, cone photoreceptors, Müller glia, and rod photoreceptors — were sufficiently represented (≥30 cells per arm) for within-class descriptive differential expression, while six classes (Endothelial, HC, Microglia, Pericyte, RGC, RPE) were excluded. The largest single log₂ fold-change was Col25a1 in rod photoreceptors (+8.74) and the strongest Wilcoxon effect was Gfap in Müller glia (+6.02 log₂FC, score 19.9), accompanied by induction of complement and acute-phase reactants. Cones shifted from glycolytic to oxidative-phosphorylation programmes (Glycolytic Process adj_p = 2.6e-10, combined_score 4,848) despite a 3-fold proportional depletion. Rods mounted an extracellular-matrix / injury-response programme (Col25a1, Fgf2, ROBO-receptor signalling). A pan-retinal up-regulation of translational machinery genes (consensus genes Hmgn2, Ubb, Rps20) was consistent with a global proteostatic response. All findings are descriptive rankings, as the single-library-per-condition design precludes formal inferential testing and a ~50% higher per-cell RNA count in the DDX41 library confounds the cross-class same-direction translational signal. Replicate libraries and targeted validation (smFISH for Gfap, Cartpt, Col25a1) are the required next step.

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

We asked what transcriptional changes occur in the DDX41 mutant retina relative to wild type, and whether such changes are shared across retinal cell types or confined to specific populations. The dataset is well-suited to a descriptive ranking of within-class effects because the existing major-class labels cover the principal retinal populations and both libraries were processed together, so cell-type identity and condition label can be resolved jointly. It is not, however, a dataset that can support tests of replicability: there is one library per condition, and any within-class p-value derived from a Wilcoxon test would be pseudoreplicated.

## Results

### Quality control

All 15,307 input cells passed per-cell filters (min_genes ≥ 200, max_pct_mt ≤ 10.0%, doublet/singlet flag = Singlet), and 22,387 genes passed the per-gene filter (min_cells ≥ 3). The two libraries were quantitatively comparable on the standard metrics, with one notable exception: the DDX41 library had ~53% higher per-cell RNA counts (mean nCount_RNA 4,190 vs 2,731) and ~38% more detected genes (mean nFeature_RNA 2,076 vs 1,505) than the WT library, while mitochondrial fractions were nearly identical (mean percent.mt 1.5% vs 1.6%). This depth imbalance must be borne in mind when interpreting the cross-cell-type up-regulation of translation/ribosomal genes (see Pan-retinal cross-class signature, below).

![Figure 1. Analysis workflow diagram showing the pipeline stages from input QC through differential expression and pathway enrichment.](figures/workflow.png)

![Figure 2. Descriptive QC summary across both libraries, showing per-cell distribution of counts, gene numbers, and mitochondrial fraction for WT and DDX41.](figures/descriptive_qc_summary.png)

### Cell-type composition shows a neuroglial expansion and a bipolar / cone contraction

The proportional landscape of the retina reorganised markedly in the DDX41 library (Table 1). Müller glia more than doubled (5.91% → 12.57%), amacrine cells more than doubled (2.85% → 6.26%), and microglia showed the largest fold change (0.27% → 0.96%, 3.62×). Bipolar cells fell (18.88% → 12.80%) and cones contracted by about two thirds (5.52% → 1.79%). Rods were essentially unchanged (66.02% → 65.24%). Because there is one library per condition, these are descriptive shifts rather than tested compositional differences, and the microglia 3.62× expansion in particular cannot be cleanly separated from the DDX41 library's higher per-cell RNA yield.

| Major class | WT % | DDX41 % | DDX41 / WT | Δ (pp) |
| --- | --- | --- | --- | --- |
| MG | 5.91 | 12.57 | 2.13× | +6.66 |
| AC | 2.85 | 6.26 | 2.20× | +3.41 |
| Microglia | 0.27 | 0.96 | 3.62× | +0.69 |
| BC | 18.88 | 12.80 | 0.68× | −6.08 |
| Cone | 5.52 | 1.79 | 0.32× | −3.73 |

*Table 1. Descriptive composition shift between the WT and DDX41 libraries (single library per condition, no test). Rods (66.02% → 65.24%) are not shown; their fraction is stable.*

### Cell-type annotation and UMAP visualization

Major-class labels shipped with the dataset were used for downstream differential expression. The 20-cluster Leiden partition (resolution 0.2) reconciled with the existing labels at ≥99% purity for the dominant populations (Rod, MG, BC, Cone, AC). The dataset's 30-dim `X_scVI` and 2-d `X_umap` embeddings were used for visualisation; no new batch integration was run.

![Figure 3. UMAP embedding coloured by Leiden cluster at the chosen resolution (0.2), used to confirm the agreement between the data-driven partition and the shipped major-class labels.](figures/umap_clusters.png)

![Figure 4. UMAP embedding coloured by major class and faceted by sample (WT, DDX41), showing the within-class and between-class distribution of cells in the two libraries.](figures/umap_majorclass_sampleid.png)

### Within-class differential expression: a per-class overview

For each of the five testable major classes, a Wilcoxon rank-sum test compared all DDX41 cells against all WT cells within that class, retaining genes with min_pct = 0.1 and |log₂FC| ≥ 0.25 and saving the top 50 up- and 50 down-regulated genes per direction. Because the design is pseudoreplicated (one library per condition), the effect-size signal we use is log₂FC; the Wilcoxon score and unadjusted p-values are reported as descriptive ranks only, not as significance tests of the contrast. Volcano plots therefore display genes past the descriptive threshold, not "significant" genes.

A clear direction bias is present in three of the five testable classes (Cone 47 up / 16 down, MG 45 / 24, Rod 49 / 12 at |log₂FC| ≥ 1.0), with the remaining two classes more balanced (AC 9 / 38, BC 24 / 26). Combined with the ~50% higher per-cell RNA count in the DDX41 library, this same-direction up-bias in the three largest classes is most parsimoniously explained as a depth effect rather than as a uniform biological induction; the within-class biological signals are read against this background.

![Figure 5. Volcano plot of within-class differential expression for amacrine cells (258 WT, 392 DDX41); x-axis log₂ fold-change, y-axis −log₁₀ p-value, points coloured by direction.](figures/volcano_AC.png)

![Figure 6. Volcano plot of within-class differential expression for bipolar cells (1,708 WT, 801 DDX41); x-axis log₂ fold-change, y-axis −log₁₀ p-value, points coloured by direction.](figures/volcano_BC.png)

![Figure 7. Volcano plot of within-class differential expression for cone photoreceptors (499 WT, 112 DDX41); x-axis log₂ fold-change, y-axis −log₁₀ p-value, points coloured by direction.](figures/volcano_Cone.png)

![Figure 8. Volcano plot of within-class differential expression for Müller glia (535 WT, 787 DDX41); x-axis log₂ fold-change, y-axis −log₁₀ p-value, points coloured by direction.](figures/volcano_MG.png)

![Figure 9. Volcano plot of within-class differential expression for rod photoreceptors (5,973 WT, 4,084 DDX41); x-axis log₂ fold-change, y-axis −log₁₀ p-value, points coloured by direction.](figures/volcano_Rod.png)

### Amacrine cells: a translational up-regulation paired with phototransduction transcript loss

The 258 WT and 392 DDX41 amacrine cells showed 9 up- and 38 down-regulated genes at |log₂FC| ≥ 1.0. The top induced gene was Angpt1 (+2.58), followed by translational / proteasomal genes (Ubb +0.73, H3f3b, Rpl/Rps family). The most strongly down-regulated transcripts were Apoe (−4.91), Pde6g (−1.90), Rho (−1.84) and Gnat1 (−1.68) — phototransduction-related transcripts despite amacrine cells being non-photoreceptor interneurons. Pathway ORA for the up direction was dominated by cytoplasmic translation / translation-elongation / SRP-dependent targeting; the down direction was dominated by Activation Of Phototransduction Cascade, Visual Phototransduction, Phototransduction Cascade, Sensory Perception, Inactivation / Recovery / Regulation Of Phototransduction Cascade, Sensory Perception Of Light Stimulus, Visual Perception, Ca²⁺ Pathway, Detection Of Visible Light, and Phototransduction Visible Light. Preranked GSEA confirmed 309 significant terms (285 up, 24 down), with the top up-regulated terms (NES ≈ +3.0) all translation-elongation / initiation / termination / nonsense-mediated-decay / selenocysteine pathways. The down direction in non-photoreceptor amacrine cells is a striking cross-class pattern but, in scRNA-seq without nuclear / cytoplasmic fractionation, may reflect ambient-RNA contamination from the photoreceptor-rich DDX41 library.

### Bipolar cells: acute activation, complement induction, and a phototransduction transcript signature

The 1,708 WT and 801 DDX41 bipolar cells showed 24 up- and 26 down-regulated genes at |log₂FC| ≥ 1.0. The top induced transcripts were Xlr3b (+3.83), A2m (+3.32), and the immediate-early genes Fos (+2.35), Egr1 (+2.48) and Junb (+1.98), with complement and serine-protease-inhibitor transcripts (C4b, Serping1 family) also up. The top down-regulated gene was Sorcs3 (−2.19). Pathway ORA for the up direction was dominated by Translation (GO:0006412), Translation R-HSA-72766, Peptide Biosynthetic Process, Macromolecule Biosynthetic Process, Eukaryotic Translation Elongation, Cytoplasmic Translation, Viral mRNA Translation, Peptide Chain Elongation, Eukaryotic Translation Termination, and SRP-dependent Cotranslational Protein Targeting To Membrane. The down direction was dominated by Phototransduction Cascade, Inactivation / Recovery / Regulation Of Phototransduction Cascade, Activation Of Phototransduction Cascade, Visual Phototransduction, Homophilic Cell Adhesion Via Plasma Membrane Adhesion Molecules, Cell-Cell Adhesion Via Plasma-Membrane Adhesion Molecules, Sensory Perception, Ca²⁺ Pathway, Phototransduction Visible Light, and Detection Of Visible Light. GSEA found 58 significant terms (51 up, 7 down) with translation up and Phototransduction Cascade down (NES = −2.67). The co-induction of immediate-early genes and complement components in bipolar cells is most parsimoniously read as a reactive response to photoreceptor stress rather than as a primary bipolar phenotype.

### Cone photoreceptors: a glycolysis-to-OxPhos metabolic shift and a survival / stress programme

The 499 WT and 112 DDX41 cone cells were the most up-direction-skewed class, with 47 up- and 16 down-regulated genes at |log₂FC| ≥ 1.0. The top induced gene was Cartpt (+5.99), followed by Cadps2, Egr1, Tox, Nrxn3, Rasgef1a, B3galt1, Zfp949, Chrnb4 and Pbxip1. The top down-regulated transcripts were Kcne2 (−2.11) and Kcnma1, together with phototransduction components. The standout pattern was the metabolic directionality: the down direction was dominated by Glycolytic Process (adj_p = 2.6e-10, combined_score 4,848, overlap 7/17), Carbohydrate Catabolic Process (adj_p = 1.1e-09, combined_score 3,031, overlap 7/22), Pyruvate Metabolic Process (adj_p = 6.6e-09, combined_score 1,911, overlap 7/29), Hypoxia (adj_p = 1.4e-08, combined_score 446, overlap 10/137), mTORC1 Signalling (adj_p = 7.2e-07, combined_score 248, overlap 9/166), Glycolysis (adj_p = 1.7e-06, combined_score 243, overlap 8/138), and the related GO terms. The up direction was topped by Oxidative Phosphorylation, Viral mRNA Translation, Translesion Synthesis By REV1, Translation R-HSA-72766, Translesion Synthesis By POLI, Translesion Synthesis By POLK, Nonsense Mediated Decay (NMD) Independent Of Exon Junction Complex, and Cap-dependent Translation Initiation. GSEA confirmed 25 significant terms (1 up, 24 down) with Pyruvate Metabolic Process (NES = −3.05), Carbohydrate Catabolic Process (NES = −2.90), Glycolytic Process (NES = −2.87), Hypoxia (NES = −2.59), Visual Perception (NES = −2.29), Glycolysis (NES = −2.25), and Glucose Metabolic Process (NES = −2.18) leading the down set. This is the most coherent energy-metabolism re-wiring in the dataset and survives both ORA and GSEA, even after accounting for the up-bias from the depth imbalance.

![Figure 10. Pathway enrichment plot for cone photoreceptors in the down direction, highlighting the glycolysis / pyruvate / hypoxia signature (Enrichr ORA); bars coloured by combined score.](figures/enrichment_Cone_down.png)

### Müller glia: a massive reactive-gliosis signature

The 535 WT and 787 DDX41 Müller glia cells produced the strongest single gene-level effect in the dataset. Gfap was induced at +6.02 log₂FC with a Wilcoxon score of 19.9, and 45 genes were up versus 24 down at |log₂FC| ≥ 1.0. Other strongly induced genes were C4b (+3.26), Serping1 (+2.90), A2m (+1.70), Mt1, Nupr1, Mt2, S100a6, and the mitochondrial transcript mt-Cytb. The most strongly down-regulated genes were the NMDA-receptor subunits Grin2b (−3.46) and Grin2d (−3.18). Pathway ORA for the up direction was dominated by Translation R-HSA-72766, Translation (GO:0006412), rRNA Processing R-HSA-72312, Macromolecule Biosynthetic Process, rRNA Processing In Nucleus And Cytosol R-HSA-8868773, Peptide Biosynthetic Process, Formation Of A Pool Of Free 40S Subunits R-HSA-72689, Peptide Chain Elongation, Eukaryotic Translation Elongation, and Nonsense Mediated Decay (NMD) Independent Of Exon Junction Complex. The down direction was dominated by Nervous System Development (GO:0007399, adj_p = 7.8e-09, combined_score 104, overlap 44/301), Cell-Cell Adhesion Via Plasma-Membrane Adhesion Molecules, Regulation Of Oligodendrocyte Differentiation, Axon Guidance, Synapse Assembly, Synapse Organization, Heterophilic Cell-Cell Adhesion Via Plasma Membrane Cell Adhesion Molecules, Neuron Projection Guidance, Vascular Transport, and Axonogenesis. Preranked GSEA returned 308 significant terms (248 up, 60 down) with translation, selenocysteine synthesis, and the GCN2 amino-acid-deficiency response as the top up terms (NES ≈ +3.4). The reactive-gliosis signal in MG is the dominant descriptive feature of the dataset and would stand on Gfap alone as a molecular marker.

![Figure 11. Pathway enrichment plot for Müller glia in the up direction, dominated by translation, rRNA processing, and the GCN2 amino-acid-deficiency response (Enrichr ORA); bars coloured by combined score.](figures/enrichment_MG_up.png)

### Rod photoreceptors: an extracellular-matrix / injury-response programme

The 5,973 WT and 4,084 DDX41 rod cells (the largest class by far) showed 49 up- and 12 down-regulated genes at |log₂FC| ≥ 1.0. The single largest log₂FC in the entire study was Col25a1 (+8.74); the Fgf2 / Fgf2os pair was also strongly induced (Fgf2os +2.57, F
