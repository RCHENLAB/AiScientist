# Dependency Management

This project intentionally keeps AiScientist and Kosmos baseline dependencies separate.

## AiScientist Environment

AiScientist should stay lightweight while the project is still defining agent boundaries.

Recommended setup:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Current runtime dependencies:

- `h5py`: required for public `.h5ad` dataset preflight, including PBMC3k.

Current development dependencies:

- `pytest`: test runner.

## Kosmos Baseline Environment

Kosmos has a much larger dependency set and should not be installed into the AiScientist environment.

Recommended temporary baseline setup:

```bash
conda create -p /private/tmp/bioagent-kosmos-baseline/conda-py311 python=3.11 pip -y
git clone https://github.com/jimmc414/Kosmos.git /private/tmp/bioagent-kosmos-baseline/Kosmos
/private/tmp/bioagent-kosmos-baseline/conda-py311/bin/python -m pip install -e /private/tmp/bioagent-kosmos-baseline/Kosmos
```

Why Python 3.11:

- Kosmos currently pins `scipy<1.14`.
- On this machine, Python 3.13 attempted to build SciPy from source and failed because no Fortran compiler was available.
- Python 3.11 can use compatible wheels for the Kosmos dependency set.

## Rules

- Do not add Kosmos dependencies to AiScientist `requirements.txt`.
- Do not commit `.env`.
- Do not commit generated run outputs.
- Keep baseline freeze files under `runs/` unless they are intentionally curated.
- If a dependency is needed only for comparison/evaluation, put it in baseline docs rather than AiScientist runtime requirements.
