# Activating scGPT + VL render-review on the deployed eyeserver

Both the scGPT annotation job and the VL (Qwen2.5-VL) render-review loop are **built and wired in
code**, but **OFF by default** and **not configured on the deployed server** — so today only the main
orchestrator (+ its text-level self-review that writes `process/report_review.md`) runs.

**Verified state (2026-07-06, `eyeserver-admin`):** `/data/BioAgent/app/.env` has **no**
`BIOAGENT_SCGPT_IMAGE` and **no** `BIOAGENT_VLREVIEW_ENABLED`; `find runs -name predictions.csv` → 0;
`console.log` has 0 `scgpt`/`constraint`/`SlurmJobError` hits. So scGPT has **never actually run**
here — any "scGPT … computational parameter constraints" wording in an old report was a report
hallucination, not a real job failure (that fabrication is fixed separately in the synthesize
grounding). The deployed code is also an **older** revision, so a redeploy is needed for it to pick up
the recent planner fixes AND (for scGPT) the "trigger on already-annotated data" change.

Deployment facts used below: app root `/data/BioAgent/app`, env `/data/BioAgent/app/.env`, venv
`/data/BioAgent/env`, systemd unit `bioagent` (`ExecStart=… -m bioagent.gateway --port 8800`).

> ⚠️ `.env` gotcha: the app **dir** may not be writable by your normal user — edit `.env` with the
> account/permissions that own it (or via the deploy flow), don't assume `vi` just works.

---

## scGPT (Route C — short-lived `gpu:1` Singularity batch job on HPC3)

1. **Build + stage the image on HPC3** (macOS cannot build a `.sif`) — follow
   [`deploy/scgpt/README.md`](scgpt/README.md): point `BIOAGENT_SCGPT_REFACTOR_SRC` at your validated
   `scGPT_refactor`, stage the model weights under `BIOAGENT_SCGPT_MODEL_DIR` on dfs3b, then
   `./deploy/scgpt/build_and_stage.sh`. It prints the `BIOAGENT_SCGPT_IMAGE` / `_ENTRYPOINT` to set.
2. **Wire it into the deployed `.env`** (`/data/BioAgent/app/.env`):
   ```
   BIOAGENT_SCGPT_IMAGE=/dfs3b/ruic20_lab/software/AiScientist/containers/scgpt.sif
   BIOAGENT_SCGPT_ENTRYPOINT=<value printed by build_and_stage.sh>
   BIOAGENT_SCGPT_MODEL_DIR=/dfs3b/ruic20_lab/software/AiScientist/scgpt_model
   # optional: BIOAGENT_SCGPT_GRES=gpu:1   (any card; decoupled from the vLLM A100 pin)
   ```
   Without `BIOAGENT_SCGPT_IMAGE` the `scgpt_annotate` tool self-reports *not configured* and is never
   used, even in a live session.
3. **Restart:** `sudo systemctl restart bioagent`.
4. **Redeploy the code too** if you want scGPT to be **auto-planned** on an already-annotated dataset
   (cross-validation) — the deployed revision predates that trigger change.
5. **Verify:** run a scRNA analysis, then on HPC3/eyeserver check a `predictions.csv` appears under the
   run's scGPT `out/` dir, and the report's annotation is grounded in it (no more "planned but did not
   execute").

## VL render-review (Qwen2.5-VL-7B — short-lived cheap-GPU batch job, opt-in)

1. **Build + stage** [`deploy/vlreview/README.md`](vlreview/README.md): `build_and_stage.sh` builds
   `vlreview.sif` and stages the 7B VL weights under `BIOAGENT_VLREVIEW_MODEL_DIR` on dfs3b.
2. **Enable in the deployed `.env`:**
   ```
   BIOAGENT_VLREVIEW_ENABLED=1
   # defaults are fine; override only if needed, e.g.:
   # BIOAGENT_VLREVIEW_IMAGE=/dfs3b/ruic20_lab/software/AiScientist/containers/vlreview.sif
   # BIOAGENT_VLREVIEW_MODEL_DIR=/dfs3b/ruic20_lab/software/AiScientist/vlreview_model
   # BIOAGENT_VLREVIEW_GRES=gpu:A30:1     # cheap 24GB card — do NOT burn an A100
   # BIOAGENT_VLREVIEW_MAX_ITERS=3        # render → review → re-render passes
   ```
3. **Restart:** `sudo systemctl restart bioagent`.
4. **Verify:** after a run's PDF renders, the gateway ships it to the VL GPU job and re-renders with
   escalated format until clean. Confirm `process/visual_review.md` appears (a Diagnostics block when
   layout defects remain; the manuscript itself stays clean per the silent-degradation design). This
   is the actual **review→FIX** loop; the always-on `report_review.md` is text-only and does not
   re-render.

## Note

The gateway currently logs to `/data/BioAgent/console.log` (journald showed **no** `bioagent`
entries), so watch that file — not `journalctl -u bioagent` — when checking a live scGPT/VL job.
