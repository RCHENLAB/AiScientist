# The hypothesis ledger can only confirm. Make it discriminate.

## What happened

Run `c135ae589d96` (Ddx41 mouse retina) had `BIOAGENT_HYPOTHESIS_DRIVEN=1`. The ledger worked
exactly as built and produced one entry:

```
h1  statement  "The transcriptional shifts between DDX41 and WT are primarily driven by a
                1.6x sequencing depth imbalance rather than genuine regulatory changes."
    prediction "Pseudobulk aggregation will collapse the massive log2FC values of the top DE genes"
    status     supported
```

Both halves of that record are broken, and neither is the model's fault.

**There was no rival.** The competing explanation — *DDX41 loss disrupts Müller-cell junctions* —
was never in the ledger, so nothing could be compared against anything. With one hypothesis in
play, every result consistent with it reads as support. "Supported" came to mean "not
contradicted", which is not what the word is for.

**The prediction could not have failed.** With one library per arm, pseudobulk returns zero
significant genes whether or not the biology is real: the outcome is fixed by the replication
structure, not by the hypothesis. A prediction the design guarantees is not a test, and marking a
hypothesis `supported` on it is circular.

The consequence was not academic. The published result for this exact model (Mars et al. 2026,
medRxiv `10.64898/2026.01.28.26344834`) is that the signal is in Müller cells, in cell morphogenesis
and junction formation — and this run's own tables carry it (`Gfap` +6.02, `Glul` −1.53 at padj
1.3e-100, `Crb1`, `Rlbp1`, `Slc1a3`, `Cdh2`, `Cadm1`, `Nlgn1` all down; MG GSEA down-terms are
cell–cell adhesion at NES −3.06). The run reported the opposite.

## What the ledger is missing

`agents/hypotheses.py` is a good store: falsifiable-by-construction fields, deterministic ids,
dedup, honest `open` status for loose ends. Its guards in `_explore_after_step` are all about **plan
hygiene** — orphan steps, duplicate steps, report busywork, caps. Not one of them is about the
**epistemic quality of the hypothesis**. So the loop grows work correctly and reasons about it
wrongly.

Three things are absent:

1. **A rival.** `Hypothesis` holds `statement / prediction / test`. The docstring for `test` says it
   should distinguish the claim "from the obvious alternative" — but the alternative is never
   stored, so nothing can check that it does.
2. **A falsifiability gate.** Nothing asks whether the proposed test *could have come out the other
   way* given this dataset's design.
3. **A contest at adjudication.** `resolve()` takes a status directly. Supporting a hypothesis
   requires only asserting it.

## The change

### 1. A hypothesis carries the explanation it is competing with

`Hypothesis` gains two fields:

| field | meaning |
|---|---|
| `rival` | the other explanation that would produce the same observation |
| `discriminator` | the outcome that favours THIS one, and the outcome that favours the rival |

`render()` prints them as a contest, so the model adjudicating at step 7 sees both sides of what it
is deciding, not one side and a status.

### 2. `admit()` — a deterministic gate in front of `add()`

The model proposes; the code decides what is admissible. Rejection reasons are returned, recorded,
and surfaced, never silently dropped:

- **no rival** — an explanation with no alternative is a narration, not a hypothesis;
- **fake rival** — the rival normalizes to the statement (restating a claim is not opposing it);
- **empty discriminator**, or one that normalizes to the prediction — no contrast is being drawn;
- **the design forces the outcome** — the case that produced h1. Given `DesignFacts` from the
  dataset profile, a discriminator that rests on a between-arm significance test is rejected when
  the design has fewer than two replicates per arm, because *both* explanations predict "nothing
  significant" there. The rule is narrow on purpose: it encodes one thing the system provably got
  wrong, rather than trying to referee falsifiability in general.

### 3. Adjudication is a verdict between two explanations

`resolve()` takes `favoured` ∈ `{hypothesis, rival, neither}` instead of a bare status, and maps it
to `supported / refuted / inconclusive`. "Supported" now requires that the evidence pushed *away*
from the rival, which is the only reading of the word that carries information.

## Scope

- `agents/hypotheses.py` — data model, gate, contest-shaped resolve, rendering. Pure: no LLM, no
  I/O, no clock, offline-testable, which is the property that makes this reviewable at all.
- `agents/research_lab.py` — the explore prompt must ask for `rival` and `discriminator`; the parse
  and the `DesignFacts` handoff from the dataset profile.
- Tests pin every rejection reason and the h1 case specifically.

## Explicitly not in scope

The Critic's second scoring axis ("is this verdict the right inference", not just "does it match the
artifact") is the other half of the same problem and is a separate line — step 6 of the DDX41 run
scored 1.0 for faithfully restating a wrong verdict. This change makes the run generate a rival;
that one makes the review notice when the rival was the right answer.
