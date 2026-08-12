#!/usr/bin/env bash
#
# Push local code to the AiScientist server with rsync (checksum / "hash" overwrite).
#
# This is the default way to ship updates: it mirrors the local repo onto the
# server, transferring only files whose CONTENT differs (rsync --checksum), and
# NEVER touches the server's secrets, venv, run outputs, or data lake.
#
# What is protected on the server (never sent, never deleted):
#   - .env / .env.local            (HPC3 config + secrets live only on the host)
#   - .venv, runs/, caches, .git   (per .gitignore + explicit excludes below)
#   - anything under $BIOAGENT_ROOT outside the app dir (we only sync the app dir)
#
# Config — set these in a gitignored ./.deploy.env (see .deploy.env.example):
#   REMOTE_HOST   server hostname / IP            (REQUIRED, no default)
#   REMOTE_USER   service account                 (default: <ucinetid>)
#   REMOTE_PORT   ssh port                         (no default — set it in .deploy.env)
#   REMOTE_APP    repo dir on the server           (default: /data/BioAgent/app)
#   SSH_KEY       path to a private key            (optional)
#
# Usage:
#   ./scripts/push.sh              # dry-run preview, then confirm, then push
#   ./scripts/push.sh -y           # push without the confirm prompt
#   ./scripts/push.sh -n           # dry-run only (show what WOULD change, exit)
#   ./scripts/push.sh --deploy     # after pushing, run remote ./deploy.sh to refresh deps
#   ./scripts/push.sh --delete     # also delete server files absent locally (still protects .env)
#
set -euo pipefail

# --- locate repo root (script lives in <repo>/scripts) -----------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

# --- load gitignored config if present ---------------------------------------
# shellcheck disable=SC1091
[ -f "$REPO_ROOT/.deploy.env" ] && source "$REPO_ROOT/.deploy.env"

REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_USER="${REMOTE_USER:-<ucinetid>}"
REMOTE_PORT="${REMOTE_PORT:-}"
REMOTE_APP="${REMOTE_APP:-/data/BioAgent/app}"
SSH_KEY="${SSH_KEY:-}"

if [ -z "$REMOTE_HOST" ]; then
  echo "ERROR: REMOTE_HOST is not set." >&2
  echo "       Create $REPO_ROOT/.deploy.env (copy from .deploy.env.example) and set REMOTE_HOST." >&2
  exit 1
fi

# --- parse flags -------------------------------------------------------------
ASSUME_YES=0
DRY_ONLY=0
DO_DELETE=0
RUN_DEPLOY=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes)    ASSUME_YES=1 ;;
    -n|--dry-run) DRY_ONLY=1 ;;
    --delete)    DO_DELETE=1 ;;
    --deploy)    RUN_DEPLOY=1 ;;
    -h|--help)   grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# --- build the ssh transport (port + optional key) ---------------------------
SSH_CMD="ssh -p $REMOTE_PORT"
[ -n "$SSH_KEY" ] && SSH_CMD="$SSH_CMD -i $SSH_KEY"

DEST="${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_APP}/"

# --- rsync filters -----------------------------------------------------------
# Order matters: PROTECT rules first so --delete can never remove server secrets,
# then honor .gitignore (so we ship what git tracks), then belt-and-suspenders
# explicit excludes for things gitignore might not cover.
RSYNC_FILTERS=(
  # never delete these on the receiver, even with --delete:
  --filter='P .env'
  --filter='P .env.local'
  --filter='P /runs/'
  --filter='P /reports/'
  # never SEND server-managed secrets/state:
  --exclude='.env'
  --exclude='.env.local'
  # ship exactly what git tracks (per-dir .gitignore merge):
  --filter=':- .gitignore'
  # explicit excludes (cheap insurance; most are already in .gitignore):
  --exclude='.git/'
  --exclude='.venv/'
  --exclude='__pycache__/'
  --exclude='*.py[cod]'
  --exclude='*.egg-info/'
  --exclude='.pytest_cache/'
  --exclude='.ruff_cache/'
  --exclude='.mypy_cache/'
  --exclude='.adaptive_kg/'
  --exclude='.playwright-mcp/'
  --exclude='.DS_Store'
  --exclude='node_modules/'
)

RSYNC_OPTS=(
  -avz            # archive, verbose, compress
  --checksum      # "hash overwrite": compare by content, not mtime/size
  --human-readable
  --itemize-changes
)
[ "$DO_DELETE" = "1" ] && RSYNC_OPTS+=( --delete )

echo "==> rsync push"
echo "    from : $REPO_ROOT/"
echo "    to   : $DEST"
echo "    ssh  : $SSH_CMD"
echo "    mode : checksum overwrite$([ "$DO_DELETE" = "1" ] && echo ' + --delete (server-only files removed, .env/runs/reports protected)')"
echo "    .env : PROTECTED (never sent, never deleted)"
echo

# --- 1) always dry-run first to preview ---------------------------------------
echo "==> preview (dry-run):"
rsync "${RSYNC_OPTS[@]}" --dry-run -e "$SSH_CMD" "${RSYNC_FILTERS[@]}" ./ "$DEST"
echo

if [ "$DRY_ONLY" = "1" ]; then
  echo "==> dry-run only; nothing was changed."
  exit 0
fi

# --- 2) confirm, then push for real ------------------------------------------
if [ "$ASSUME_YES" != "1" ]; then
  printf "Proceed with the push above? [y/N] "
  read -r reply
  case "$reply" in
    y|Y|yes|YES) ;;
    *) echo "aborted."; exit 0 ;;
  esac
fi

echo "==> pushing..."
rsync "${RSYNC_OPTS[@]}" -e "$SSH_CMD" "${RSYNC_FILTERS[@]}" ./ "$DEST"
echo "==> push complete."

# --- 3) optionally refresh server deps ---------------------------------------
if [ "$RUN_DEPLOY" = "1" ]; then
  echo "==> running remote deploy.sh to refresh venv/deps..."
  $SSH_CMD "${REMOTE_USER}@${REMOTE_HOST}" "cd '$REMOTE_APP' && ./deploy.sh"
fi

echo "==> done."
