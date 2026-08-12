# Free clinical text → HPO terms (`map_phenotype_to_hpo`)

**Status:** built, unit-tested, wired into the Scientist catalog, **deployed**, and the LLM half is
**verified against the real Qwen3.6-35B-A3B** — 5/5 including the family-history and no-phenotype traps
(2026-07-15; it took a prompt fix to get there — see [What is not verified](#what-is-not-verified)).

## Why this exists

LIRICAL and Exomiser need the patient's phenotype as **HPO IDs**. Clinicians do not write HPO IDs —
they write free text, in whatever language and shorthand the clinic uses:

> 10 岁男孩，自幼夜盲，视野缩窄，ERG 呈熄灭型，无听力障碍。

and the lab's own solved-case sheet writes the diagnosis as `RP with macular involvement`, `BBS`,
`Choroidal dystrophy`, `Pattern Dystrophy`.

Before this tool, `run_lirical` took `hpo_terms` straight from the orchestrator model — i.e. the model
was asked to *remember* HPO IDs. That is the one job an LLM must never have here:

> `HP:0000662` is **Nyctalopia**. `HP:0000622` is **Blurred vision**.

A transposition apart, both real eye phenotypes. A wrong-but-real ID **fails silently** — nothing
errors, LIRICAL simply conditions the posterior on the wrong phenotype and returns a confident, wrong
differential. That is worse than a crash, because it is reportable.

## The design: the LLM does language, the ontology owns identity

| # | Stage | Who | What |
|---|---|---|---|
| 1 | EXTRACT | **LLM** | free text → clinical phrases + negation. Translation (中文→English), shorthand expansion (`ERG 熄灭型` → `nonrecordable electroretinogram`), splitting compounds, catching `no hearing loss` as **negated** and `her mother had RP` as **family history** (dropped). |
| 2 | RETRIEVE | code | each phrase → real candidates from the HPO index (`index.py`), plus the curated IRD alias table for shorthand the ontology has no name for. |
| 3 | SELECT | **LLM** | picks a **candidate number** from that closed list, or `0` for none. It never types an ID, so it cannot invent one. Exact lexical hits skip this step. |
| 4 | VALIDATE | code | every surviving ID is re-checked against the index; the canonical name comes from the **ontology**, never the model. Unknown → dropped, obsolete → forwarded to `replaced_by`, both reported. |

Same closed-set grounding pattern as the report writer's anti-fabrication layers (`research_lab._grounding_facts`).

Every mapped term carries the phrase and the `method` that produced it, so a clinician can **audit**
rather than trust:

```
[+] HP:0000662   Nyctalopia            <- '自幼夜盲' (llm_closed_set)
[-] HP:0000365   Hearing impairment    <- '无听力障碍' (ABSENT)
[?] unmapped: 'family history of consanguinity'
```

### Precedence in `map_phrase` (most precise first)

1. **`ontology_exact`** — the phrase *is* an HPO label/synonym → unambiguous, no LLM needed.
2. **`curated_alias`** — `ird_hpo.tsv`, for clinical shorthand HPO has no name for (`Stargardt` →
   Macular dystrophy, `BBS`, `RP`). Runs **second on purpose**: it is hand-written and coarser than the
   ontology (its `cone dystrophy` row points at *Cone/cone-rod dystrophy* while HPO names *Cone
   dystrophy* exactly), so it must not pre-empt a term the ontology names.
3. **`llm_closed_set`** — the LLM's pick from the retrieved candidates.
4. **`lexical_only`** — high-confidence (≥0.80) lexical top-1, only when no LLM is reachable.

Below that bar a phrase is left **unmapped and reported** rather than guessed: an unmapped phrase is
visible and recoverable, a wrong term is neither.

## The ontology data

`hpo_lexicon.tsv.gz` (≈390 KB) is **generated and committed**, so the tool works offline, in tests, and
on the eye server with no HPC3 and no network:

```bash
python scripts/build_hpo_lexicon.py            # download the current hp.json, regenerate
python scripts/build_hpo_lexicon.py --hp-json /path/to/lirical/data/hp.json
```

- Scope: the `HP:0000118` (Phenotypic abnormality) subtree — 19,120 current terms + ~23k exact synonyms
  — plus 577 obsolete terms (so a stale ID is explained, not just missing). Inheritance / modifier /
  frequency subtrees are excluded: they are not phenotypic features.
- **Not eye-only.** Syndromic IRD needs the rest of the body (Usher → hearing loss, BBS → polydactyly,
  obesity, renal).
- The HPO release (currently `2026-06-23`) rides in the file header and is returned as `hpo_version` on
  every mapping, so any result traces to an exact ontology version. HPO is CC BY 4.0.
- `BIOAGENT_HPO_LEXICON` overrides the bundled file.

### Alignment with LIRICAL's ontology — verified, and guarded

Two ontologies are in play: **ours** resolves the free text into IDs, **LIRICAL's staged `hp.json`**
resolves those IDs into disease likelihoods. They must be the same release.

**Verified aligned 2026-07-15**: HPC3's `/dfs3b/ruic20_lab/software/reference/lirical/data/hp.json` is
release `2026-06-23` and **byte-identical** to the file the bundled lexicon was built from
(`md5 e4ce3ae038165fe65f8c45ad46a29922` on both). No rebuild was needed.

They are updated by different hands, though — ours by re-running `build_hpo_lexicon.py` and committing,
LIRICAL's by whoever next runs `lirical download` — and nothing keeps them matched. **Drift is silent**:
a term we still map to may be obsolete in a newer LIRICAL ontology, and it would simply stop matching —
no error, just a quieter differential. So `run_lirical` compares the two releases on every call
(`hpo_release_drift`) and reports a mismatch in `phenotype_notes` with the exact regeneration command.
The check is free: the release stamp sits in `hp.json`'s header, so it is read with a single 1 MB read
rather than parsing the 23 MB file.

## The gate on `run_lirical`

The model can still ignore the tool and type IDs. So `run_lirical` validates **every** incoming ID
before it can define a patient's phenotype:

- unknown / malformed → **dropped**, reported in `phenotype_notes`;
- obsolete → **forwarded** to its replacement, reported;
- none valid → **error** pointing at `map_phenotype_to_hpo`;
- labels in the phenopacket are overwritten with the ontology's, so provenance records what the ID
  actually means rather than what the model thought it meant.

## Behaviour on the lab's real case sheet

All 8 distinct `Diagnosis` strings map with **no LLM at all** (locked in
`tests/test_hpo_mapper.py::test_maps_every_diagnosis_in_the_real_case_sheet`):

| Diagnosis (as written) | → | Method |
|---|---|---|
| `Choroidal dystrophy` | HP:0001135 Chorioretinal dystrophy | curated_alias — HPO has no "choroidal dystrophy" |
| `Pattern Dystrophy` | HP:0007963 Pattern dystrophy of the retina | curated_alias |
| `Stargardt` | HP:0007754 Macular dystrophy | curated_alias |
| `BBS` | HP:0000556 Retinal dystrophy | curated_alias |
| `RP with macular involvement` | HP:0000510 Rod-cone dystrophy | curated_alias |
| `Achromatopsia` / `Cone dystrophy` / `Macular dystrophy` | HP:0011516 / HP:0008020 / HP:0007754 | ontology_exact |

Note what a disease-name alias does **not** do: `BBS` yields Retinal dystrophy, *not* the syndrome's
obesity/polydactyly/renal features. Those are real BBS features but are not stated by the text, and
inventing findings a clinician did not write is how a phenotype-driven differential quietly becomes
fiction. LIRICAL's own HPO/OMIM annotations supply what the disease implies.

## The pipeline that uses it

`preset_pipelines/phenotype_variant_diagnosis/` — the **VCF + case description** protocol
(`data_type: variants`, alongside `variant_annotation` which stays the VCF-only path). Two independent
tracks that meet at a reconcile step: `annotate_variants` → variant shortlist, and
`map_phenotype_to_hpo` → `run_lirical` → per-disease posterior. `run_lirical` does NOT consume the
annotation — in genotype-aware mode it scores the raw VCF through its own Exomiser DB — so the tracks
are parallel, and their **disagreement is often the finding** (a variant-only hit means the gene isn't
in LIRICAL's curated annotations, not that it isn't causal).

**The "if and only if" the protocol promises is enforced in code, not just in the prompt:** with no
phenotype text `map_phenotype_to_hpo` returns no observed terms (it calls `infer_hpo_terms` with
`default=False`, so the old `HP:0000556` "always have a phenotype" default can never fire), and
`run_lirical` errors on an empty `hpo_terms`. A VCF with no case description therefore *cannot* be
scored, whatever the model decides to call.

The converse — that the phenotype step always runs *when* a description IS present — is still only
steered by the prompt + the router's one-liner. See "deterministic triggering" below.

### The case note: the second attachment slot

The description can come from the **question text** or from an **attached case note** (the console's
"Attach a case note" item). `map_phenotype_to_hpo` reads the attachment when called with no `text`; an
explicit `text` argument always wins (the model may be mapping one specific sentence). The result
reports `text_source` (`argument` | `attached_case_note`) so it is auditable.

**Why it is TEXT on the run request (`LabRequest.case_note`), not an upload.** A run binds exactly ONE
dataset (`dataset_path` is a scalar; `models.py` has a single `dataset_id` FK), and for a case that slot
must hold the VCF — a note uploaded as a dataset would displace it. The note escapes that ceiling
because of one property: **its only consumer runs in-process on the gateway.** `map_phenotype_to_hpo` is
deliberately not in `_HPC_PHENOTYPE_TOOLS`, so the note never has to be bind-mounted into a Slurm
container. The browser reads the file's text and posts it with the run; nothing is staged, no dataset row
is created, no bind set changes. It is capped at 64k chars (truncated, not rejected — a long referral
letter must not fail a run) and persisted into `run_state.json` so a resume keeps the phenotype.

This deliberately covers **text notes only** (`.txt`/`.md`). A second *data* file — a BED panel, a second
VCF — is still the hard problem: it would have to be bind-mounted into the job, so it needs the bind set
(`slurm_analysis.py` `binds_ro`), the in-container `run_tool(tool, workspace, dataset_path, args)`
contract, and the dataset FK changed together. `extra_ro_binds` is the natural seam.

## What is not verified

- ~~The LLM extract stage against a real model.~~ **DONE 2026-07-15 — 5/5 on the real
  Qwen3.6-35B-A3B** (`scripts/hpo_mapper_smoke.py --openrouter`, model `qwen/qwen3.6-35b-a3b`, the same
  model HPC3 serves). Both predicted failure modes HELD: the family-history trap ("his **mother** had
  retinitis pigmentosa") did NOT leak HP:0000510 into the proband's phenotype, and a note with no
  phenotype at all invented nothing.

  It took a prompt fix to get there, and the first run is worth recording. The extract prompt listed
  the negated *phrasings* as examples ('no hearing loss', '无听力障碍'), and Qwen reasonably read those
  as what the `phrase` field should look like — so it emitted `{"phrase": "no hearing loss"}`, which
  matches nothing in an ontology that names findings rather than their absence, and the exclusion was
  silently lost. **Note the failure DIRECTION: it surfaced as an `unmapped` phrase, not a wrong term** —
  the closed set held, the miss was visible, and the smoke test caught it. The prompt now shows the
  conversion explicitly (`"无听力障碍"` → `{"phrase": "hearing impairment", "negated": true}`), and
  after the fix even `"Hearing is normal"` — which the first run missed entirely — comes back as a
  correct exclusion.

  Re-run any time:
  ```bash
  PYTHONPATH=src python scripts/hpo_mapper_smoke.py --openrouter        # reads OPENROUTER_API_KEY from .env
  PYTHONPATH=src python scripts/hpo_mapper_smoke.py --port <tunnel> --model qwen3.6:35b-a3b
  ```

- **End-to-end against the solved cases** (text → HPO → LIRICAL → does the known gene rank?). Needs
  HPC3 + the gated LIRICAL deploy. Caveat worth stating up front: several sheet cases have
  *unexpected* genes for the diagnosis (`TMEM67` for Stargardt, `NBAS` for Achromatopsia, `WASF3` for
  RP). LIRICAL scores against curated HPO/OMIM annotations, so it may rank those **low** — that is the
  literature/evidence track's job (`phenotype_gene_confidence_rag_spec.md`), not a mapping bug. Do not
  read a low LIRICAL rank on those cases as a failure of this tool.

- **Deterministic triggering.** Nothing forces the phenotype step when a study description contains
  symptoms; the model still decides to call the tool. That remains the top open item on the phenotype
  line (see `handoff/yijun/HANDOFF.md`).
