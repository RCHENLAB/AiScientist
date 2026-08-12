---
name: perturbation_analysis
description: Analyze a pooled CRISPR / Perturb-seq screen — rank which perturbations change the transcriptome, then per-perturbation DE vs the non-targeting control
tools: run_scanpy_qc, run_clustering, run_de, run_enrichment, run_code, literature_search
---

# Pooled-Perturbation (Perturb-seq) Analysis Protocol

**Purpose.** Take a pooled CRISPR screen where each cell carries a **guide / perturbation label**
(Perturb-seq / CROP-seq / CRISPRi-a) and learn WHICH perturbations reshaped the transcriptome and
HOW. The defining feature: **many perturbation groups share ONE non-targeting control**, and a large
fraction of guides have **no detectable effect** — so the first analytical question is "which
perturbations actually did something?", *not* "run DE on all of them". The pipeline ranks perturbation
strength (E-distance to control) first, then runs per-perturbation DE only on the guides with a real
phenotype.

|  |  |
|---|---|
| **Input** | one scRNA-seq AnnData where each cell carries a CRISPR **guide/perturbation label** in `obs` |
| **Output** | a perturbation **E-distance ranking** (which guides did something + which are silent) + **per-perturbation DE** tables vs the shared control (+ optional enrichment / module grouping / citations) |
| **Engine** | `scanpy` (QC / PCA / DE) via the registered tools + scPerturb **E-distance** & **Mixscape** `run_code` templates in the analysis image; `pertpy` optional; large screens on HPC3 (`BIOAGENT_RUN_CODE_ON_HPC=1`) |
| **Not for** | cell-type assignment (use `celltype_annotation`) or a simple two-group condition comparison (use `differential_expression`) |

> **How to read the "parameters" in each step.** Two kinds of knob feed this pipeline, and the protocol
> keeps them separate on purpose:
> - **🔬 Agent-chosen (scientific)** — the PI/Scientist decides these per study and you should audit them:
>   the **perturbation column** and the **non-targeting control level** (both read from the dataset's own
>   `obs`), whether you work at **guide or target level**, the **min-cells** cutoff that flags unstable
>   guides (default `< 30`), the E-distance **FDR / permutation count**, and which perturbations are
>   "strong" enough to carry into enrichment.
> - **⚙️ Fixed (infra)** — the execution substrate; it never changes the science: the analysis image
>   already ships **scanpy / numpy / pandas** (the E-distance + DE templates use only these); **pertpy**
>   is a heavy **optional** dependency used *only* by Mixscape (Step 5); large screens should set
>   `BIOAGENT_RUN_CODE_ON_HPC=1` for a real `--mem` cap. Shown where relevant (Steps 4–6), not re-audited
>   per run.

---

## At a glance — the 9 steps

| # | Step | Tool / skill | 🔬 Key agent-chosen params | Output |
|---|------|--------------|----------------------------|--------|
| 1 | **QC the cells** | `run_scanpy_qc` | filter/normalize thresholds; **keep control cells** | filtered + log1p AnnData (+ HVG) + counts |
| 2 | **Embedding** | `run_clustering` | (agent-chosen `n_pcs` / neighbors) | PCA embedding + UMAP-by-perturbation |
| 3 | **Define the screen** | *(from DATASET PROFILE)* | perturbation column · control level · guide\|target · min cells (`<30`) | screen definition + per-perturbation cell counts |
| 4 | **Rank perturbation strength (E-distance)** | `run_code` · `perturbation_edistance.py` | control level · FDR · `N_PERM` | ranked E-distances + **silent-guide** list |
| 5 | **(Optional) Remove escaping cells** | `run_code` · `mixscape_escape_filter.py` | run Mixscape or not | `mixscape_class_global` (NP cells droppable) |
| 6 | **Per-perturbation DE vs control** | `run_code` · `perturbation_de_vs_control.py` | `reference` = control level · guide\|target | per-perturbation DE + self-knockdown check |
| 7 | **(Optional) Pathway enrichment** | `run_enrichment` | up/down gene SYMBOL sets of strong hits | enriched pathways per perturbation |
| 8 | **(Optional) Group perturbations by effect** | `run_code` · `perturbation_edistance.py` (pairwise) | — | perturbation×perturbation E-distance matrix / modules |
| 9 | **(Optional) Literature context** | `literature_search` | gene + program query | citations |

> The heavy compute lives inside the `run_code` **E-distance** (Step 4) and **DE** (Step 6) templates;
> `run_de` is per-cluster one-vs-rest and **cannot** do the shared-control comparison, so it is not used
> for the DE step. Steps 5, 7, 8, 9 are genuinely **optional** — take them only when the data supports
> them. The final methods + results report is assembled automatically — **do NOT plan a report-writing
> step.**

### Reading the screen from the data (do this even with a vague/empty question)

The dataset's own metadata defines the screen. Read the **DATASET PROFILE** (obs columns + their
category values are given at planning time) and infer:

- **(a) The perturbation column** — a usually **high-cardinality** obs column naming the guide/target per
  cell (`perturbation`, `guide`, `gRNA`/`sgRNA`, `target`/`target_gene`/`gene_target`, `knockout`,
  `condition`, `KO`, `feature_call`). Its many levels are the perturbation groups. High cardinality
  (dozens/hundreds of guides) is the **tell** that separates a screen from a 2-group condition dataset.
- **(b) The non-targeting control level** — the level naming a control guide (`NT`, `non-targeting`,
  `control`, `ctrl`, `NTC`, `sgLacZ`, `sgGFP`, `sgScramble`, `scramble`, `unperturbed`, `none`). Every
  perturbation is compared **against this shared control**. State which level you chose; if genuinely
  ambiguous, ask ONE clarify question in plan mode rather than guessing.
- **(c) Guides vs targets** — if the column is per-**guide** (`TP53_sg1`, `TP53_sg2`), you may collapse
  to the **target gene** for power (concordant sgRNAs of the same gene are biological replicates) — do
  that when guide-level groups are small. Say whether you analyzed at guide or target level.
- **(d) No perturbation column?** — if guide identity is NOT in `obs` (it lives in a separate guide-count
  layer / `obsm` / a CITE-seq-style feature matrix), cells must be **assigned to a guide first**
  (max-count / demultiplexing) as a required `run_code` pre-step. **Do not fabricate a `perturbation`
  column.**

---

## Step-by-step

<details open>
<summary><b>Step 1 · QC the cells</b> — are the cells usable — and did you keep the controls?</summary>

**What.** `run_scanpy_qc`: per-cell metrics, cell/gene filtering, normalize + log1p (and HVG). Report
the cell/gene counts before and after.

**Why.** Standard scRNA-seq QC — but with one perturbation-specific rule: every perturbation is scored
**against the shared control**, so dropping the control cells breaks the whole screen. Honor QC columns
already present in the data rather than blindly recomputing.

**🔬 Agent-chosen:** the filter/normalize thresholds (agent-chosen per study); **do NOT drop the control
cells**; respect any existing QC columns.

**What runs** — the registered QC tool call:
```
run_scanpy_qc  →  per-cell QC metrics · cell/gene filters · normalize_total + log1p · (optional HVG)
```

**✅ Verify this step:** the control cells are still present after QC · cell/gene counts reported
before→after · pre-existing QC columns are honored, not silently overwritten.

<sub>Source: `preset_pipelines/perturbation_analysis/SKILL.md` (Ordered plan §1)</sub>
</details>

<details>
<summary><b>Step 2 · Embedding</b> — build the space the effect-size ranking measures distances in</summary>

**What.** `run_clustering`: compute PCA / neighbors / UMAP.

**Why.** The **PCA embedding is what the E-distance ranking (Step 4) measures distances in**, so it is a
hard prerequisite for Step 4. A UMAP colored by perturbation is a useful overview figure. Leiden clusters
themselves are **secondary here** — the grouping of interest is the **perturbation label**, not de-novo
clusters.

**🔬 Agent-chosen:** none load-bearing (agent-chosen `n_pcs` / neighbor count per study).

**What runs** — the registered clustering tool call:
```
run_clustering  →  PCA · neighbors · UMAP
```

**✅ Verify this step:** a PCA embedding exists (it feeds Step 4) · a UMAP colored by the perturbation
label is produced · de-novo Leiden clusters are not treated as the primary grouping.

<sub>Source: `preset_pipelines/perturbation_analysis/SKILL.md` (Ordered plan §2)</sub>
</details>

<details>
<summary><b>Step 3 · Define the screen</b> — name the perturbation column, the control, and the level</summary>

**What.** From the DATASET PROFILE, fix the screen design (see *Reading the screen from the data* above):
the **perturbation column**, the **non-targeting control level**, and whether you work at **guide or
target level**. Then **count cells per perturbation** and **flag the perturbations with too few cells**
(default `< 30`) — they give unstable DE and E-distance.

**Why.** This is the analytical frame the rest of the pipeline runs against: many perturbations share ONE
control, and a large fraction of guides do nothing. Getting the control level wrong mis-references every
downstream comparison; a low-count guide produces noise that masquerades as a phenotype.

**🔬 Agent-chosen:** the perturbation column · the control level (state which you chose; if genuinely
ambiguous, ask ONE clarify question, don't guess) · guide-vs-target level · the min-cells flag
(default `< 30`).

**What runs** — read the profile (no tool call), then count cells per perturbation. If guide identity is
**not** in `obs`, a `run_code` **guide-assignment pre-step** (max-count / demultiplexing) must run first —
never fabricate a `perturbation` column.

**✅ Verify this step:** the chosen perturbation column + control level are stated · the guide-vs-target
choice is stated · per-perturbation cell counts are reported and low-count (`<30`) guides flagged · no
invented perturbation column when guide identity is absent.

<sub>Source: `preset_pipelines/perturbation_analysis/SKILL.md` ("Figuring out the perturbation design" + §3) · `references/methods.md`</sub>
</details>

<details>
<summary><b>Step 4 · Rank perturbation strength</b> — which guides actually did something (E-distance + permutation test)</summary>

**What.** `run_code`, adapting `perturbation_edistance.py`: compute the scPerturb **E-distance** (energy
distance, in **PCA space**) of each perturbation to the non-targeting control, with a **permutation test**
for significance, and **rank** perturbations by effect size. Perturbations NOT significantly different
from control are **silent** (the guide may not have knocked down, or the gene is dispensable in this
context) — **report them as silent, don't run downstream biology on them.**

**Why.** A large fraction of guides have no detectable effect. Ranking by E-distance focuses the rest of
the analysis on the perturbations with a **real transcriptional phenotype** — answering "which
perturbations actually did something?" before spending DE on all of them.

**🔬 Agent-chosen:** the control level (the reference) · the **FDR** threshold · the permutation count
`N_PERM`. *(agent-chosen per study)*

**What runs** — `run_code` with the E-distance template. E-distance between perturbation P and control C:
```
E(P,C) = 2·δ_PC − δ_PP − δ_CC ,   δ_XY = mean over pairs of ‖x − y‖²
```
Computed **in closed form** from group means/variances (`δ_XX = 2·Var(X)`;
`δ_PC = ‖μ_P − μ_C‖² + Var(P) + Var(C)`), so it is **exact and O(n·d)** — no n×n pairwise matrix, scales to
large screens. Significance is a **label-permutation test**: pool P+C, reshuffle the two labels `N_PERM`
times, count how often the permuted E-distance ≥ observed (Davison–Hinkley `+1`), then **Benjamini–Hochberg**
across perturbations. Not significant at the chosen FDR ⇒ **silent**.

> **⚠️ Statistic definition.** With squared-Euclidean cost the statistic is **centroid-shift-based** — that
> is the scPerturb definition and what pertpy's `Distance metric="edistance"` reports. A
> distribution-shape-sensitive variant would need non-squared Euclidean + subsampled pairwise distances.

**✅ Verify this step:** E-distances **and** permutation / BH-adjusted p-values are reported (not just gene
names) · perturbations are ranked by effect size · silent guides are listed **as a result**, not dropped
silently and not forced into DE.

<sub>Source: `skills/perturbation_edistance/reference.py` · `references/methods.md`</sub>
</details>

<details>
<summary><b>Step 5 · (Optional) Remove escaping cells (Mixscape)</b> — escaped cells look like control and dilute DE</summary>

**What.** `run_code`, adapting `mixscape_escape_filter.py`: within a perturbation, some cells **escape**
knockdown and look like control, diluting DE. Mixscape (pertpy) labels perturbed vs **non-perturbed (NP)**
cells so the NP cells can be dropped before DE.

**Why.** Removing the escaped-cell dilution sharpens the DE signal. But **`pertpy` is a heavy optional
dependency** — if it is unavailable, **skip this step** and say the DE was run on **all guide-assigned
cells** (a conservative, effect-diluting choice). Do not block the pipeline on it.

**🔬 Agent-chosen:** whether to run Mixscape at all (optional).

**What runs** — `run_code` with `pt.tl.Mixscape`: a local perturbation signature (cell minus nearest
control neighbours) + a per-perturbation mixture model classifies each cell as perturbed (KO/KD),
**non-perturbed (NP = escaped)**, or NT. The stable output key is `obs["mixscape_class_global"]`. The
template **degrades to "skip + run DE on all cells"** if pertpy is unavailable (its API drifts across
versions).

**✅ Verify this step:** if run — NP (escaped) cells are identified via `mixscape_class_global` and dropped
before DE · if skipped — the report SAYS the DE ran on all guide-assigned cells (conservative, diluting)
and states pertpy was unavailable · the step never blocks the pipeline.

<sub>Source: `skills/mixscape_escape_filter/reference.py` · `references/methods.md`</sub>
</details>

<details>
<summary><b>Step 6 · Per-perturbation DE vs the control</b> — the shared-reference comparison run_de cannot do</summary>

**What.** `run_code`, adapting `perturbation_de_vs_control.py`: for **each perturbation with a real effect
and enough cells**, run perturbation-vs-non-targeting-control DE (scanpy `rank_genes_groups` with an
**explicit `reference` = the control level**, looped over perturbations). `run_de` is **per-cluster
one-vs-rest**, so it does NOT do this shared-reference comparison — use the template.

**Why / sanity check.** A bona-fide knockout should show its **OWN target gene down** in its DE — report
that **self-knockdown as a positive control**, and **flag guides where the target does NOT drop** (possible
mislabel / no editing).

**🔬 Agent-chosen:** `reference` = the control level · run only for perturbations with a real effect (from
Step 4) and enough cells (Step 3) · the guide-vs-target level carries through.

**What runs** — `run_code`:
```
rank_genes_groups(..., groups=[pert], reference=CONTROL, method="wilcoxon")   # looped over perturbations
```
Report **adjusted p-values + log fold-changes**. The template checks `target_self_knockdown` per
perturbation and flags failures.

> **⚙️ Memory discipline (infra, not science).** Load the AnnData **ONCE**; inside the per-perturbation loop
> subset with a **view** (`adata[mask]`) — never `.copy()` every group or hold all subsets at once. An
> over-budget loop is OOM-killed on the local sandbox (`returncode == -9`). Prefer
> `BIOAGENT_RUN_CODE_ON_HPC=1` for a real `--mem` cap on large screens.

**✅ Verify this step:** DE used an **explicit `reference` = control** (shared reference), not per-cluster
one-vs-rest · each perturbation reports adjusted p-values + log fold-changes · target **self-knockdown** is
reported as a positive control and guides failing it are flagged · silent / low-cell perturbations are
excluded.

<sub>Source: `skills/perturbation_de_vs_control/reference.py` · `references/methods.md`</sub>
</details>

<details>
<summary><b>Step 7 · (Optional) Pathway enrichment per strong perturbation</b> — what program did each strong guide move?</summary>

**What.** `run_enrichment` on the **up-/down-gene sets** of each perturbation with a substantial DE result.
Send gene **SYMBOLS only**.

**Why.** Interpret the transcriptional program each strong perturbation induced.

**🔬 Agent-chosen:** which perturbations count as "substantial DE"; the up vs down gene sets fed in.

**What runs** — `run_enrichment` on the up/down gene-symbol sets.

**✅ Verify this step:** only gene **symbols** are sent · enrichment is run per strong perturbation's
up/down sets · resulting pathways are framed as hypotheses.

<sub>Source: `preset_pipelines/perturbation_analysis/SKILL.md` (Ordered plan §7)</sub>
</details>

<details>
<summary><b>Step 8 · (Optional) Group perturbations by transcriptional effect</b> — correlated signatures = shared module</summary>

**What.** Perturbations with **correlated DE signatures** (or **small pairwise E-distance**) likely act in
a **shared pathway / module**. The `perturbation_edistance.py` template can also emit a pairwise
**perturbation×perturbation E-distance matrix** for this.

**Why.** Surface candidate co-functional modules. **Frame shared modules as hypotheses.**

**🔬 Agent-chosen:** none load-bearing.

**What runs** — `run_code` with `perturbation_edistance.py` in pairwise mode → a
perturbation×perturbation E-distance matrix.

**✅ Verify this step:** modules are derived from correlated DE / small pairwise E-distance · they are
framed as hypotheses, not established pathways.

<sub>Source: `skills/perturbation_edistance/reference.py` · `preset_pipelines/perturbation_analysis/SKILL.md` (Ordered plan §8)</sub>
</details>

<details>
<summary><b>Step 9 · (Optional) Literature context</b></summary>

**What.** For the strongest hits, pull real citations linking the perturbed gene to the observed program
via `literature_search`.

**Why / honesty.** Cite **only** what the tool returns — never invent a PMID or a claim.

**✅ Verify this step:** every citation resolves to a returned record · claims are attributed.

<sub>Source: `preset_pipelines/perturbation_analysis/SKILL.md` (Ordered plan §9)</sub>
</details>

---

## Grounding & honesty (applies to every step)

Report **effect sizes** (E-distance, log fold-change) and **ADJUSTED p-values / permutation p-values** —
never just gene names. State the **control level**, whether you worked at **guide or target level**, and
**every perturbation skipped for low cell count**. **Silent perturbations are a result, not a failure** —
report which guides had no detectable effect rather than forcing DE on them. Use the target-gene
**self-knockdown as a positive control** and flag guides that fail it (possible mislabel / no editing).
Never fabricate a gene, fold-change, pathway, or perturbation. Frame biology as **hypotheses to validate,
not established fact**.

## Method provenance

scanpy (QC · PCA / neighbors / UMAP · `rank_genes_groups`) via the registered tools · scPerturb
**E-distance** (Peidli et al., *Nat. Methods* 2024), computed in closed form from group means/variances
with a label-permutation test + Benjamini–Hochberg · **Mixscape** (Papalexi et al., *Nat. Genetics* 2021;
`pertpy`, optional). The `run_code` templates are adapted from the **k-dense-ai/scientific-agent-skills**
perturbation references and the scPerturb / pertpy literature; they use only what the analysis image ships
(scanpy / numpy / pandas), with **pertpy** as an optional heavy dependency (Mixscape only). Full method
notes: [`references/methods.md`](references/methods.md).

<sub>This protocol's formulas and command excerpts are pulled from the cited source functions —
regenerate to keep them in step with what runs.</sub>
