# scGPT annotation assets

scGPT annotation runs through the pinned `scgpt-annotation` MCP server. The application no
longer builds or dispatches `scgpt.sif`; its container recipe and inference wrapper live in
[aiscientist-mcp](https://github.com/liujs123456/aiscientist-mcp/tree/main/containers/scgpt-annotation).
The reviewed MCP catalog selects the image digest, entrypoint, tool schema and GPU resources.
See [MCP deployment](../../docs/development/mcp_bridge.md) for image provisioning.

The model and validated `scGPT_refactor` harness remain external operator-managed assets.
They are not downloaded, generated or added to this repository. Keep the existing lab assets:

- The reference model directory contains `best_model.pt`, `vocab.json`, `id2type.json` and
  `dev_train_args.yml`. `AISCIENTIST_SCGPT_MODEL_DIR` identifies the gateway's shared model
  asset; `SCGPT_MODEL_DIR` selects the model directory for the native server.
- `SCGPT_REFACTOR` points to the validated harness directory containing
  `step1_preprocess.py`, `step2_inference.py` and their supporting files.
  The server copies the harness into each task's private workspace and runs its existing
  preprocessing and inference steps. The source harness and model are mounted read-only.

For example, configure the operator's environment with the actual staged paths:

```bash
SCGPT_MODEL_DIR=/absolute/shared/models/scgpt
SCGPT_REFACTOR=/absolute/shared/scGPT_refactor
```

The `scgpt-annotation` catalog entry must allow these exact names in `settings_env`.
Model-provided arguments cannot select an executable or an arbitrary model directory.
`AISCIENTIST_SCGPT_GRES` still controls the gateway's GPU allocation preference.

Stage large assets through the cluster's transfer node. Image pulls and inference run in
Slurm compute allocations, including `--nv` for GPU inference. The former app-owned image
recipe, build script and gateway job dispatcher have been retired; existing cluster images
and model files are not deleted by that code migration.
