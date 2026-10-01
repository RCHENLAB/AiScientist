#!/usr/bin/env bash
#
# Deploy / refresh the AiScientist console on a host (e.g. the eye server).
#
# Run as the SERVICE account (<ucinetid> in dev, aiscientist in prod) — NOT root.
# One-time prerequisite per host (needs admin, this is the ONLY sudo step):
#     sudo apt install -y python3-venv python3-full
#
# Idempotent: re-run any time to update deps after new code lands. Everything is
# env-configurable so nothing is tied to one person or host:
#     AISCIENTIST_ROOT    base dir              (default /data/BioAgent)
#     AISCIENTIST_APP     app/repo dir          (default $AISCIENTIST_ROOT/app)
#     AISCIENTIST_ENV     venv dir              (default $AISCIENTIST_ROOT/env)
#     AISCIENTIST_PYTHON  python to build venv  (default python3)
#     AISCIENTIST_EXTRAS  pip extras            (default gateway; add biomni for the
#                                             research backend, analysis for the
#                                             scanpy/gseapy stack used by run_code +
#                                             the analysis line — e.g. gateway,biomni,
#                                             analysis, auth for user accounts/login.
#                                             System tools also need (apt): pandoc
#                                             texlive-xetex (PDF) + graphviz (schematics)
#                                             + postgresql (accounts DB, for the auth extra))
#     AISCIENTIST_PORT    console port          (default 8800)
#
# Usage:
#     ./deploy.sh            # create/update the venv + install deps
#     ./deploy.sh --run      # ...then start the console in the foreground
#
set -euo pipefail
# Legacy BIOAGENT_* names still work (AISCIENTIST_* wins when both are set) — same rule as the
# Python side (aiscientist.core.config.apply_brand_env_aliases).
for _old in $(compgen -v BIOAGENT_ || true); do _new="AISCIENTIST_${_old#BIOAGENT_}"; [ -n "${!_new+x}" ] || export "$_new=${!_old}"; done

ROOT="${AISCIENTIST_ROOT:-/data/BioAgent}"
APP="${AISCIENTIST_APP:-$ROOT/app}"
ENV_DIR="${AISCIENTIST_ENV:-$ROOT/env}"
PYTHON="${AISCIENTIST_PYTHON:-python3}"
EXTRAS="${AISCIENTIST_EXTRAS:-gateway}"
PORT="${AISCIENTIST_PORT:-8800}"

echo "==> AiScientist deploy"
echo "    app    : $APP"
echo "    venv   : $ENV_DIR"
echo "    extras : $EXTRAS"
echo "    python : $("$PYTHON" --version 2>&1)"

if [ ! -d "$APP/src/aiscientist" ]; then
  echo "ERROR: $APP is not the repo (no src/aiscientist). rsync or git clone it first." >&2
  exit 1
fi

# 1. venv (create if missing) -------------------------------------------------
if [ ! -x "$ENV_DIR/bin/python" ]; then
  echo "==> creating venv at $ENV_DIR"
  if ! "$PYTHON" -m venv "$ENV_DIR" 2>/tmp/aiscientist-venv-err; then
    cat /tmp/aiscientist-venv-err >&2 || true
    echo "ERROR: venv creation failed. Install the venv module once (admin):" >&2
    echo "       sudo apt install -y python3-venv python3-full" >&2
    exit 1
  fi
fi
# shellcheck disable=SC1091
source "$ENV_DIR/bin/activate"
echo "==> pip: $(command -v pip)"

# 2. dependencies (editable: aiscientist importable, no PYTHONPATH needed) --------
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
