#!/usr/bin/env bash
#
# Production redeploy: push local code to eyeserver, then restart the systemd service.
# This is the frequent-update workflow once the console runs under systemd (see
# deploy/systemd/bioagent.service) behind nginx.
#
#   ./deploy/redeploy.sh           # rsync + restart + show the live version fingerprint
#   ./deploy/redeploy.sh -n        # dry-run the rsync only (no restart)
#
# Frontend (HTML/JS/CSS) is served from disk per request — for a UI-only change you do NOT
# need this; just `./scripts/push.sh -y` and refresh the browser. Use redeploy for backend
# (.py) changes, which need the worker to restart.
#
# Reads the same gitignored ./.deploy.env as scripts/push.sh (REMOTE_HOST/USER/PORT/APP).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"
# shellcheck disable=SC1091
[ -f "$REPO_ROOT/.deploy.env" ] && source "$REPO_ROOT/.deploy.env"
# Legacy BIOAGENT_* names still work (AISCIENTIST_* wins when both are set) — same rule as the
# Python side (aiscientist.core.config.apply_brand_env_aliases).
for _old in $(compgen -v BIOAGENT_ || true); do _new="AISCIENTIST_${_old#BIOAGENT_}"; [ -n "${!_new+x}" ] || export "$_new=${!_old}"; done
REMOTE_HOST="${REMOTE_HOST:?set REMOTE_HOST in .deploy.env}"
REMOTE_USER="${REMOTE_USER:-<ucinetid>}"
REMOTE_PORT="${REMOTE_PORT:-}"
SERVICE="${AISCIENTIST_SERVICE:-bioagent}"

if [ "${1:-}" = "-n" ]; then
  exec ./scripts/push.sh -n
fi

echo "==> pushing code to $REMOTE_USER@$REMOTE_HOST ..."
./scripts/push.sh -y

echo "==> restarting $SERVICE on the server ..."
ssh -p "$REMOTE_PORT" "$REMOTE_USER@$REMOTE_HOST" \
  "sudo systemctl restart $SERVICE && sleep 3 && systemctl is-active $SERVICE && \
   journalctl -u $SERVICE -n 20 --no-pager | grep -aE 'fingerprint|Uvicorn running' || true"
echo "==> redeploy complete."
