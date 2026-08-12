# Per-Agent Isolated + Evolving Memory (Axis C) — Design + v1

**Status: v1 IMPLEMENTED (flag-gated, DAG only).** `src/bioagent/agents/agent_memory.py` +
`ResearchLab._run_one_node` (read before acting, write an episode after) + end-of-run reflection.
Gated on `LabConfig.agent_memory` + `agent_memory_dir`; gateway env `BIOAGENT_AGENT_MEMORY` (DAG mode).
Validated: offline (`tests/test_agent_memory.py` + 2 in `test_research_lab.py`) AND real cross-run
learning on Qwen3.6 via OpenRouter (`scripts/dag_memory_openrouter.py`: run 1 distilled QC lessons to
disk, run 2 recalled them into the expert's brief). This is the priority upgrade that turns "one model
playing scripted personas" into agents with independent, persistent, *evolving* state — without new
hardware. Prioritised ABOVE dynamic re-planning (`dag_planner_design.md` §8), which changes research
direction and carries drift risk; per-agent memory deepens each role's competence without changing
direction (low drift).

**Not yet done (v2):** semantic (embedding) retrieval instead of keyword/recency; a lab-wide shared
"vetted lessons" pool; memory in the linear loop (today DAG only); reflection cost controls (only
reflect agents with ≥N new episodes).

## 1. Honest baseline — what exists vs what's missing

- **Exists:** per-STEP context isolation (each Specialist's tool-loop is its own conversation);
  a per-step goal string in the persona (`Your goal: …`); a SHARED accepted-findings digest passed
  forward within a run (`_accepted_findings_block`).
- **Missing:** PERSISTENT, PER-AGENT memory that (a) survives across steps AND across runs, (b) is
  PRIVATE to each agent (not the shared digest), and (c) EVOLVES (a reflection pass distils raw
  episodes into reusable lessons).
- **NOT missing / NOT needed:** separate models or GPUs. Agents share ONE served model instance;
  "agent" = independent evolving STATE, not independent hardware.

## 2. Compute budget (the hard constraint) — fits on 1× 80G A100

- All agents TIME-SHARE the one served LLM (Qwen3.6-35B-A3B AWQ, ~20 GB weights) on the A100 that
  already hosts the orchestrator — vLLM continuous batching serves many concurrent agent contexts.
  Adding agents + memory ≈ **0 extra VRAM**.
- The memory store is **CPU/disk** (JSONL episodes + a distilled `lessons.md` per agent). Retrieval
  starts as keyword+recency (no model); an OPTIONAL small embedding model (~0.1–0.5 B, e.g. bge-small)
  for semantic recall runs on CPU or <2 GB VRAM.
- A distinct specialist MODEL (e.g. a cheap 7 B for routine sub-agents) is an OPTIONAL optimisation —
  it co-locates on the same 80 G card or a second card. NOT required for "real agents."
- **Net: 1× A100 is sufficient. No extra hardware. The orchestrator stays always-on on the A100
  (already the case).**

## 3. Architecture

```
per agent (stable id: pi, critic, qc_specialist, clustering_specialist, pathway_specialist, …)
  memory/<agent_id>/
    episodes.jsonl     # append-only: {ts, run_id, node, action_digest, critic_verdict, key_numbers}
    lessons.md         # distilled, evolving heuristics (human-readable, human-editable)
  scope key = <agent_id> [+ optional <domain/dataset> so retina-QC lessons ≠ blood-QC lessons]
```

- **AgentMemory** (new small module): `read(agent_id, query, budget) -> str` and
  `write_episode(agent_id, episode)` and `reflect(agent_id) -> updates lessons`.
- **Read (before acting):** inject THIS agent's top-K relevant lessons + recent episodes into its
  scoped brief — retrieved by relevance to the node goal (keyword/recency first; vector later).
  Private: agent A never sees B's memory.
- **Write (after a node terminates):** append one episode (what it did, the Critic verdict, the key
  numbers). Private per agent.
- **Evolve (reflection):** at end of run (or every N episodes) a bounded LLM call per agent distils
  raw episodes into updated `lessons.md`. This is what makes memory EVOLVE, not just accumulate.
  Cheap, time-shared on the A100. (Mirrors the existing "consolidate memory" pattern.)

## 4. Isolation boundary (the point of this work)

- **Private, per-agent:** each agent's `episodes.jsonl` + `lessons.md` — a separate namespace. No
  agent can read or write another's memory.
- **Shared (unchanged) coordination channel:** the blackboard — accepted findings + on-disk
  checkpoints — stays the ONLY cross-agent handoff (edges of the DAG). So: **private evolving memory
  per agent + shared task state.** Clean separation; independence without losing coordination.

## 5. Integration with the DAG (slots in cleanly)

- `_run_one_node`: `AgentMemory.read` → prepend to the scoped brief; on terminal,
  `AgentMemory.write_episode`.
- `_claim_specialist`: may consult memory ("which expert has relevant prior experience with this
  kind of task/dataset?").
- Reflection: a post-run step (or the existing consolidate-memory hook).
- Fully additive + flag-gated (`LabConfig.agent_memory: bool = False`); empty memory ⇒ today's
  behaviour (graceful cold start).

## 6. Risks + guards

- **Memory poisoning / drift:** a wrong lesson could perpetuate errors. Guards: reflection runs the
  distillation through a critic; lessons are ADVISORY (steer, not bind); memory is plain-text and
  **human-inspectable/editable**; cap injected tokens (reuse the context budgeter).
- **Context bloat:** retrieve top-K only, token-capped.
- **Over-isolation:** agents wouldn't learn from each other — acceptable; cross-agent learning is a
  later option (a shared "lab lessons" pool distinct from private memory).
- **Cross-run scope:** key memory by agent (+ optional domain) so lessons transfer to similar future
  runs but don't leak across unrelated datasets.

## 6b. Clarifications (from review)

**Two tiers of memory — long-term memory is NOT the context window.**

```
LONG-TERM (disk)                 WORKING (context window, rebuilt each call)
memory/<agent>/episodes.jsonl    [persona] + [retrieved memory slice] + [scoped brief] + [tool loop]
memory/<agent>/lessons.md   ── retrieve top-K ─►  (bounded by the model's context length)
unbounded / durable / cheap      ephemeral / reconstructed per call / discarded after
```

The disk store is NOT in context by default; before an agent acts, retrieval selects only the few
relevant KB and injects them. This DECOUPLES "how much the agent knows" (unbounded, disk) from "how
much fits in one call" (bounded, context) — the exact cure for today's crammed-in findings digest +
trimming. (This is RAG applied to agent memory.)

**How many agents share the A100 — it is NOT partitioned per agent.** One served model
(Qwen3.6-35B-A3B AWQ, ~20 GB) is the substrate ALL roles time-share. "Agent count" is a LOGICAL role
count (a design choice — ~5–6 today: PI, Critic, 3 default specialists / a 2–4-member team; grow at
will, ~0 extra VRAM). Only 1 (sequential) or 2–3 (concurrent) are ever in-flight at once. The "always
-on PI/orchestrator" = the served model is always up; the PI is one of its callers, not a process
owning a VRAM slice. VRAM is not the binding constraint at this scale.

**Why memory EVOLVES — it is CROSS-RUN learning, not in-step correction.** Two distinct mechanisms at
two time scales:
- **In-step correction = the Critic→revise loop** (seconds, within one step). Already exists; NOT this.
- **Memory evolution = reflection** (days/weeks, ACROSS runs). Within a single run a step executes
  once and is done — no evolution there. Evolution shows up on the NEXT run: run 1 the QC agent sets
  mito<10% and the Critic flags it; that episode is stored; run 2 (new dataset / next day) the QC
  agent retrieves the lesson and starts at mito<5%. The model weights are frozen — memory is how the
  agents "learn" operationally WITHOUT fine-tuning. Reflection distils raw episodes into a compact,
  retrievable lesson set so memory crystallises instead of growing into an unusable log.
- **Payoff scales with run-volume × role-reuse:** small over 3 runs, real over 100. Low drift (it
  deepens role competence; it does not change research direction).

## 6c. Storage location, retention & compression

**Where it lives: the gateway (eyeserver), NOT HPC3/Singularity.** The orchestration loop (ResearchLab
/ PI / Critic / Scientist / claim / reflection) runs on the gateway; only specific TOOLS (scanpy,
run_code) offload to HPC3 Slurm jobs. Memory read (into the prompt) + write (an episode) happen around
each LLM call = gateway-side. The Singularity analysis containers are the wrong home: network-off,
EPHEMERAL (destroyed per job), read-only bind-mounted source — a compute sandbox, not a memory home.
So memory lives beside `runs/console/<owner>/` and `ssh_creds/<owner>/`, e.g.
`<data>/agent_memory/<owner>/<agent_id>/`. OPTIONAL: sync/back up to HPC3 dfs3b (persistent lab
storage) for durability — but the PRIMARY home is eyeserver-local (reading a remote FS over SSH before
every LLM call would add unacceptable latency).

Scope: key by `<owner>` (+ optional domain) — per-user isolation first (safest). A lab-wide "shared
lessons" pool for VETTED, methodological heuristics is a later option; dataset-specific numbers/donor
ids must NEVER enter a lesson (lessons are method heuristics, not data).

**Compression — three points (the store is bounded, not a growing log):**
1. **Reflection (semantic compression, the main one):** many raw episodes → a few crystallised
   `lessons.md` heuristics. Lossy on detail, keeps the actionable signal.
2. **Retention/rotation (mechanical):** raw episodes are ARCHIVED (not deleted) once distilled;
   `lessons.md` is deduped/merged and capped to top-N.
3. **Retrieval (read-time):** inject only top-K relevant, token-capped.

Guard: keep archived raw episodes so a distillation can be re-run or audited; lessons are plain-text
and human-editable.

**Frozen weights — external memory only steers, it does not teach.** No fine-tuning / no gradient
updates: this is in-context learning via retrieval. The full per-turn loop is
`retrieve → inject → LLM executes → write episode → (periodically) reflect/compress`. Ceiling: memory
can bias behaviour the model is already capable of ("prefer mito<5%"); it cannot give the model a
skill it fundamentally lacks — that would need fine-tuning (a separate, more expensive path).

## 7. What this does and does NOT make it

- **Does:** give each agent persistent, independent state that shapes future behaviour — the
  defining property of an agent (state + policy). This is the real step from "personas" to "agents,"
  on one GPU.
- **Does NOT (by itself):** direct agent-to-agent messaging/negotiation (the blackboard is the
  coordination channel — a legitimate multi-agent pattern; direct messaging is optional later), nor
  dynamic re-planning (§8, deliberately deferred — higher drift risk).
