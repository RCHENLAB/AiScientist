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
