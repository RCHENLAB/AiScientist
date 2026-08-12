# A/B — does the PROTOCOL format plan better than SKILL.md?

**Question.** `SKILL.md` is the file the agent actually loads. A second, researcher-auditable rendering
(`PROTOCOL.md`) was prototyped. If the protocol format *also* planned as well or better, it could
replace SKILL.md internally. Does it?

**Verdict: no measurable benefit, a measurable cost, and the test could not tell them apart.** The
deterministic scorer tied them at a perfect score — which means it **saturated**, not that they are
equivalent. On the one signal that did vary (an LLM judge), the protocol format scored **worse**, and it
costs ~35–40 % more prompt tokens.

**Provenance.** `experiments/protocol_format/ab_test.py`, two runs of 3 trials per condition on
`qwen/qwen3.6-35b-a3b` via OpenRouter, planning an IRD / GRCh37 WGS-VCF study. Raw artifacts
(`raw.json`, `rows.json`, `run1.log`, `run2.log`) were **gitignored** — the numbers below are rescued
here because they otherwise existed only in one throwaway worktree.

---

## How it was scored

Two scorers. The deterministic one was intended as the reliable signal.

```
det_score = coverage + params_ok + order_ok − anti_patterns          max = 11
```

**There are no weights — every component is worth exactly 1 point.**

| Component | Max | What earns a point |
|---|---|---|
| **coverage** | 7 | one per phase present: qc · normalise · narrow · annotate · summarise · prioritise · literature |
| **params_ok** | 3 | used `max_pop_af=0.01` · recognised assembly as GRCh37 · used a gene panel |
| **order_ok** | 1 | `annotate` comes *after* qc / normalise / narrow |
| **anti_patterns** | −3 | −1 each: added a report-writing step · collapsed to <4 steps · defaulted to GRCh38 without noting GRCh37 |

The optional LLM judge separately rated **completeness / param_correctness / faithfulness** (0–10 each,
never combined into a weighted total) plus a readability score.

---

## Results

### Deterministic scorer — a perfect tie

| Condition | det_score | coverage | steps | anti-patterns |
|---|---|---|---|---|
| **OLD_skill** | **11.0 / 11** | 7 / 7 | 7 | 0 |
| **NEW_protocol** | **11.0 / 11** | 7 / 7 | 7 | 0 |

Every one of the 3 trials × 2 runs scored 11/11 in both conditions.

> **⚠️ This is a ceiling effect, and it invalidates "they are equivalent" as a conclusion.** A scorer
> where both arms max out has no resolution left; all it licenses is *"this rubric cannot distinguish
> them."* To actually compare the formats the rubric has to get harder — e.g. include items that can
> only be answered by following a progressive-disclosure pointer into `references/`.

### LLM judge — the protocol format scored worse

Per-trial (run 2; run 1's judge failed, see limitations):

| Condition | trial | completeness | param_correctness | faithfulness | judge flagged |
|---|---|---|---|---|---|
| OLD_skill | 0 | 8 | 6 | 6 | `report_tables` |
| OLD_skill | 1 | 10 | 10 | 10 | — |
| OLD_skill | 2 | 10 | 10 | 10 | — |
| **OLD_skill avg** | | **9.3** | **8.7** | **8.7** | 1 of 3 trials |
| NEW_protocol | 0 | 8 | 7 | 6 | `report step in step 5` |
| NEW_protocol | 1 | 8 | 7 | 6 | `step 3 combines narrowing and annotation` |
| NEW_protocol | 2 | 8 | 9 | 6 | `report` |
| **NEW_protocol avg** | | **8.0** | **7.7** | **6.0** | **3 of 3 trials** |

**The most concrete difference:** under the protocol format the model invented a **report-writing step in
3 of 3 trials** (both formats explicitly forbid one — the report is assembled automatically). The old
skill format did it in 1 of 3.

Note that the **deterministic scorer missed this entirely** — its anti-pattern regex looks for
`write report|manuscript|generate report`, which the model's phrasings ("report", "report_tables") did
not match. So the tie at 11/11 partly reflects a **blind spot in the rubric**, not clean behaviour.

### Readability (LLM rating as a non-CS researcher) — also a tie

| Condition | readability | can audit steps? |
|---|---|---|
| OLD_skill | 8 / 10 | yes |
| NEW_protocol | 8 / 10 | yes |

**The protocol format's whole premise is that it is better for a human to audit — and this test did not
detect that either.** That is not evidence the premise is wrong; it is evidence an LLM judge is the
wrong instrument for it. Settling it needs the advisor reading both, not another model.

### Cost

| Run | OLD_skill | NEW_protocol | overhead |
|---|---|---|---|
| 1 | 9,642 chars (~2,410 tok) | 13,097 chars (~3,274 tok) | **+36 %** |
| 2 | 9,942 chars (~2,485 tok) | 13,995 chars (~3,498 tok) | **+41 %** |

---

## Limitations (read before citing any of this)

- **Ceiling effect** — both arms scored maximum on the primary metric; the rubric cannot discriminate.
- **N = 3 per condition, judge on a single run.** Run 1's judge failed outright
  (`anthropic/claude-3.5-sonnet` → HTTP 404, no endpoint on OpenRouter), so all judge numbers come from
  one run of 3 trials. The faithfulness gap (8.7 vs 6.0) is the most interesting signal and the least
  well-supported.
- **LLM-as-judge**, not a human reviewer — and the thing the protocol format is *for* is human review.
- Single task (one IRD WGS-VCF planning prompt), single model.

## What this supports

Keep `SKILL.md` as the single machine-read source and keep `PROTOCOL.md` as the human-facing rendering —
which is also what **operon** does (its `protocols/<name>/` folders contain a `SKILL.md` plus
`scripts/` + `references/`; "protocol" is the directory, not a rival format). Do **not** migrate the
agent-facing path to the protocol format on the strength of this experiment: it shows no gain, a token
cost, and a possible faithfulness regression.
