# Run stage timing

Open **Runs → Timing** to inspect a run's stages, including an interrupted run or a saved plan
awaiting review. Reopen the panel to refresh it. It shows UTC start/end timestamps, elapsed
seconds, category, outcome and decision action. The first 500 spans are shown; **Full timing JSON**
contains the complete trace. Runs created before timing was enabled have no retrospective trace.

The authenticated endpoint is `GET /api/timing/{owner}/{run_id}`. The owner and administrators
can read it, using the same access boundary as result downloads. With accounts disabled, the
deployment is a single-operator service, as for existing downloads. The journal is also in
`<results>/<owner>/<run_id>/artifacts/process/timing.jsonl` and **Results (.zip)**. It remains
available without a live connection. It follows the run's existing retention/deletion policy.
Because it continues to grow after a result is published or saved, the timing journal is excluded
from the result's immutable Croissant file checksum inventory; research-result metadata is unchanged.

Requests receive a durable id before routing. If a request continues an older run, its timing
segment is appended to that run with a stable `request_id` and `segment_id`. The original trace
retains a `run_link` event naming the final `target_run_id`; the final run's endpoint includes the
attached segment. The original linked trace can remain partial and must not be counted again.

## Reading a timeline

Each span has `span_id`, `stage`, `category`, `started_at`, `ended_at`, `duration_seconds`,
`outcome`, `partial`, and safe `metadata`. `started`/`ended` are Unix timestamps. Ordinary elapsed
durations use a monotonic clock. Recovered review waits and later-save delays use wall-clock
timestamps and say `duration_basis=wall_clock`; clock corrections can affect these intervals.

| Stage | Meaning |
| --- | --- |
| `plan_generation` | Planning events until the plan card or execution; repeated revisions are separate spans. This enclosing observation can include a nested confirmation wait. |
| `user_review` | A displayed plan, clarification, decision or confirmation waiting for its user. The accepted action is recorded explicitly; an HTTP 200 or cleared card does not imply approval. |
| `model_call` | Observed wall duration of the gateway model entry point, including its recovery/retries, shared-model wait or nested confirmation. This is not exclusive compute or GPU busy time. Role identifies routing, planning, writing or chat. |
| `data_staging`, `data_transfer` | Staging and transport operations, including interrupted copying. Nested transport spans may overlap. |
| `corpus_load`, `corpus_wait`, `corpus_search` | Cold local loading, contention/retrieval wait, and actual local search. Cached loading is not charged again; a concurrent waiter records only its own wait. |
| `tool_call` | Tool entry/exit observed by the existing run monitor; this enclosing observation can include human confirmation and nested transfers or jobs. |
| `hpc_submit` | Gateway submission duration. |
| `hpc_queue_observed`, `hpc_job_observed` | Gateway polling intervals, categorized as observations. They are not Slurm accounting or measured execution time. |
| `hpc_queue`, `hpc_execution` | Slurm `sacct` Submit→Start and Start→End when available. Unknown timestamps stay null. Start→End is allocation wall time, not CPU/GPU utilization; a requeued job's Submit describes the latest allocation. |
| `result_packaging` | Writing the literature answer, result pointers/tables and result metadata. Several packaging operations can occur in one run. |
| `save_decision_wait` | Published `result_ready` to a subsequent manual save request. It is a user wait, separate from compute and from the actual save. No delay is inferred from a result pointer's `created_at`. |
| `result_save` | Actual foreground save operation or background copy/publish. A foreground `queued` outcome means HTTP 202; the background span records the copy's eventual outcome. Automatic saving has no manual decision wait. |

**Do not add span durations to get wall time.** A model, transfer and independent DAG tools may
overlap, and outer spans contain inner spans. Categories distinguish leaf processing, `user_wait`,
and inclusive `observation` spans; they are not an exhaustive partition. Uninstrumented gaps remain unattributed.
Gateway model spans do not claim per-request timing for models inside a remote corpus job.

`result_ready` marks publication of the result files, not completion of a search. Search/model
spans supply those boundaries. A later save may finish after `run_complete` or while another chat
is running; its explicit owner/run identity remains the original one. A saved plan can similarly
be decided after its original worker exits or after a gateway restart.

## Partial runs and production diagnosis

Starts and ends are appended immediately, only on transitions (never every poll or token).
Each write is closed; complete lines survive a process restart and a torn line is skipped. This
is best-effort local storage, not a power-loss/fsync guarantee. Failure to write timing never
changes normal execution. An open processing span remains `partial=true`, with null end/duration
and unknown outcome, until an actual completion is observed. Reading does not invent a crash
timestamp or call a WebSocket disconnect a run failure. A run's terminal event and a background
operation's terminal event can legitimately differ.

The service journal emits `run_timing run_id=… stage=… event=… action=… outcome=…` on transitions.
Timing records contain no prompts, raw commands, file paths, credentials or private payloads.
For investigation, retain the run id, conversation id, timing JSON, deployment revision/configuration,
and any available Slurm job accounting. Check the explicit action and stage before attributing a gap.

Offline tests reproduce long stages, cancellation, review waits, concurrent runs, restart recovery
and delayed saves with controlled clocks and fake transports. They establish instrumentation
behavior, not a production performance baseline. A real slow run still needs a fresh trace after
deployment; these timings alone do not establish which stage is a production bottleneck or prove
any speed improvement.
