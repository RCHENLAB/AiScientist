# Contributing to AiScientist

## Workflow

- Fork the repository, work on a branch, and open a pull request. CI runs the byte-compile check,
  ruff (advisory), the Croissant validation and the full offline test suite on every pull request.
- Keep commit titles short and specific: say what changed, not "update" or "sync".
- Run the tests before you push. They are offline and need no cluster, `.env` or network:

  ```bash
  python -m pip install -e ".[gateway]" -r requirements-dev.txt
  python -m pytest
  ```

## Testing on HPC3

The offline tests check the logic against a scripted cluster. Only HPC3 itself shows queue waits,
modules, images, file systems and quotas, so a change to Slurm jobs, SSH, staging or HPC3 storage
is also tried there. The gateway on your own computer stands in for the lab server: you need no
account on it.

- Copy `configs/aiscientist.example.env` to `.env` and fill it in. Give your jobs a name prefix of
  your own, such as `AISCIENTIST_SLURM_JOB_PREFIX=dev-`, so your gateway never reuses or cancels
  the website's jobs for your account.
- Start it with `./start.sh` and sign in with your own UCI HPC3 account. Off campus this needs the
  UCI VPN. Jobs run as your HPC3 user and are charged to the Slurm accounts in `.env`.
- Follow RCIC's rules: login nodes only log in and submit jobs, compute runs in Slurm jobs, and
  files move through `access-hpc3.rcic.uci.edu`. Commands you type yourself follow them too.
- Use public or test data, or lab data you are allowed to read. Data stays on UCI systems.
- In the pull request, list what ran on HPC3: the job ids, the partition and the data.

## Rules every change follows

- **Data has Croissant metadata.** Every new dataset type, pipeline, or result type produces valid
  Croissant 1.1 metadata, generated in code and validated in CI. A feature is not done until it
  does. The guideline is [`docs/development/metadata.md`](docs/development/metadata.md).
- **Tools stay behind their contract.** A tool is one folder under `src/aiscientist/tools/` and
  imports only `aiscientist.tools.sdk`; see [`src/aiscientist/tools/README.md`](src/aiscientist/tools/README.md).
  After changing a tool's schema or description, run `python scripts/tool_docs.py`.
- **No credentials in the repository.** Passwords, API keys, tokens and private keys belong in the
  deployment's `.env` or state directory. Use a placeholder such as `<password>` in documentation.
  `scripts/pr_review_gate.py` checks every pull request.
- **Documents describe the current state.** README files and the docs under `docs/` say what is
  true now. A version's changes go in the GitHub release notes.
