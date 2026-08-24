# Does splitting the evidence change what the team argues about?

Both arms run the SAME interpretation meeting on the SAME six findings, against the real served
Qwen3.6, with the same three experts and the same synthesis prompt. The only variable is whether
each expert is dealt its own slice.

The findings are built so a member holding only part of them would reach a different conclusion —
a strong per-cell signal (1,204 DE genes, Reactome adj-p 1e-12) sitting next to the fact that
`orig.ident` has ONE level and pseudobulk refused every cell type. Real studies produce this
conflict constantly.

## What actually differed

**SHARED — every expert sees all six findings.** The team argued about *methodology in general*:

> Cell-level covariate-adjusted DE vs pseudobulk/hierarchical modelling … Bioinformatician favours
> `scVI`, `MAST`, `DESeq2`; Biostatistician favours pseudobulk with retina as random intercept

Note what is cited: scVI, MAST, DESeq2, smFISH, R-loops — **none of which are in the evidence**.
Given the same material, the experts fall back on shared textbook priors, and the disagreement is
a best-practice debate that would read the same for any dataset. Each expert saw both the strong
signal and the missing replication, and privately reconciled them before speaking.

**SPLIT — each expert holds a different slice.** The team argued about *this study's evidence*:

> **Bioinformatician:** the Rod signature (1,204 genes, FDR<0.05, OXPHOS up / synaptic down) is
> coherent and robust — moderate confidence.
> **Biostatistician, Biologist, Critic:** at n=1 the p-values are invalid; the signature is
> statistically meaningless.
> **Evidence cited:** Bioinformatician cites the Wilcoxon output and Reactome adj-p 1e-12; the
> others cite `t2.csv`/`t5.csv` (design showing n=1) and `t3.csv` (pseudobulk refusal).

That is a positional conflict anchored to **who examined which table** — and the member arguing the
signal is real is not wrong from its slice, which is exactly why the disagreement carries
information. The AGREED section moved the same way: SHARED produced generic rules ("use structured
FDR", "tier your confidence"), SPLIT produced findings about this dataset (7 of 12 subclasses
covered, hematopoietic extrapolation unjustified, downgrade to exploratory).

## Caveats

* **n=1 per arm.** One meeting each. The difference is large and in the predicted direction, but
  this is an illustration, not a measured rate.
* **It isolates the evidence split ONLY.** The AGREED/UNRESOLVED structure comes from the synthesis
  prompt, which is active in BOTH arms — so SHARED already surfaces *a* disagreement. What the
  split changed is *what the disagreement is about*. On this evidence the synthesis prompt looks
  like the larger single lever; the split is what makes the argument about the data.

## Reproducing

```bash
python experiments/meeting_asymmetry/run_meeting_ab.py --port <tunnelled vLLM port>
```
