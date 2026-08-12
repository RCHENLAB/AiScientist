#!/usr/bin/env bash
#
# Deploy / refresh the AiScientist console on a host (e.g. the eye server).
#
# Run as the SERVICE account (<ucinetid> in dev, bioagent in prod) — NOT root.
# One-time prerequisite per host (needs admin, this is the ONLY sudo step):
#     sudo apt install -y python3-venv python3-full
#
# Idempotent: re-run any time to update deps after new code lands. Everything is
# env-configurable so nothing is tied to one person or host:
#     BIOAGENT_ROOT    base dir              (default /data/BioAgent)
#     BIOAGENT_APP     app/repo dir          (default $BIOAGENT_ROOT/app)
#     BIOAGENT_ENV     venv dir              (default $BIOAGENT_ROOT/env)
#     BIOAGENT_PYTHON  python to build venv  (default python3)
#     BIOAGENT_EXTRAS  pip extras            (default gateway; add biomni for the
#                                             research backend, analysis for the
#                                             scanpy/gseapy stack used by run_code +
#                                             the analysis line — e.g. gateway,biomni,
#                                             analysis, auth for user accounts/login.
#                                             System tools also need (apt): pandoc
#                                             texlive-xetex (PDF) + graphviz (schematics)
#                                             + postgresql (accounts DB, for the auth extra))
#     BIOAGENT_PORT    console port          (default 8800)
#
# Usage:
#     ./deploy.sh            # create/update the venv + install deps
#     ./deploy.sh --run      # ...then start the console in the foreground
#
set -euo pipefail

ROOT="${BIOAGENT_ROOT:-/data/BioAgent}"
APP="${BIOAGENT_APP:-$ROOT/app}"
ENV_DIR="${BIOAGENT_ENV:-$ROOT/env}"
PYTHON="${BIOAGENT_PYTHON:-python3}"
EXTRAS="${BIOAGENT_EXTRAS:-gateway}"
PORT="${BIOAGENT_PORT:-8800}"

echo "==> AiScientist deploy"
echo "    app    : $APP"
echo "    venv   : $ENV_DIR"
echo "    extras : $EXTRAS"
echo "    python : $("$PYTHON" --version 2>&1)"

if [ ! -d "$APP/src/bioagent" ]; then
  echo "ERROR: $APP is not the repo (no src/bioagent). rsync or git clone it first." >&2
  exit 1
fi

# 1. venv (create if missing) -------------------------------------------------
if [ ! -x "$ENV_DIR/bin/python" ]; then
  echo "==> creating venv at $ENV_DIR"
  if ! "$PYTHON" -m venv "$ENV_DIR" 2>/tmp/bioagent-venv-err; then
    cat /tmp/bioagent-venv-err >&2 || true
    echo "ERROR: venv creation failed. Install the venv module once (admin):" >&2
    echo "       sudo apt install -y python3-venv python3-full" >&2
    exit 1
  fi
fi
# shellcheck disable=SC1091
source "$ENV_DIR/bin/activate"
echo "==> pip: $(command -v pip)"

# 2. dependencies (editable: bioagent importable, no PYTHONPATH needed) --------
pip install -U pip
case ",$EXTRAS," in *,biomni,*) echo "    note: biomni's first run downloads an ~11GB data lake" ;; esac
echo "==> pip install -e $APP[$EXTRAS]"
pip install -e "$APP[$EXTRAS]"

# 3. config sanity ------------------------------------------------------------
if [ ! -f "$APP/.env" ]; then
  echo "WARNING: $APP/.env not found — the console needs HPC3 config. Start from the template:" >&2
  echo "         cp $APP/configs/aiscientist.example.env $APP/.env   # then edit" >&2
fi

# 4. done ---------------------------------------------------------------------
echo "==> ready."
echo "    start the console:   ./start.sh        (or ./deploy.sh --run)"
echo "    view from a laptop:  ssh -p <ADMIN_SSH_PORT> -L $PORT:localhost:$PORT <you>@<this-host>  then open http://localhost:$PORT/"

if [ "${1:-}" = "--run" ]; then
  exec "$APP/start.sh"
fi
