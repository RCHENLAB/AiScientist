#!/usr/bin/env bash
#
# deploy_interactive.sh — coworker-friendly deploy. NO key / NO sudoers setup needed.
#
# You just type your own `<you>-admin` account password when prompted. It rsyncs YOUR
# local working tree to the eyeserver and restarts the console as the `bioagent` service
# account. (For an automated/keyed/NOPASSWD deploy instead, use scripts/sync_deploy.sh.)
#
# How it avoids repeated password prompts:
#   - one SSH "master" connection is opened (you type your SSH password ONCE);
#   - your code is rsync'd to a staging dir you own (no sudo);
#   - ONE `ssh -t` step runs as bioagent via sudo (you type your sudo password ONCE) to
#     copy staging -> the app dir and restart.
# So at most two prompts, both labelled. If your -admin account uses the same password for
# login and sudo, it's just that one password, twice.
#
# WHY rsync (not git pull): the server can't fetch the PRIVATE repo (no creds on the box),
# so we push your local state directly.
#
# USAGE:
#   scripts/deploy_interactive.sh [--no-delete] [--allow-dirty]
#     --no-delete    keep server files that aren't in your local tree (default: --delete,
#                    i.e. make the server match your local EXACTLY — recommended).
#     --allow-dirty  deploy even with uncommitted local changes.
#
# ENV: reads the SAME gitignored ./.deploy.env as scripts/sync_deploy.sh (so BIND_HOST etc. are
#      set once, in one place); anything below can also be overridden inline. Defaults:
# ENV (defaults): HOST=<GATEWAY_HOST>  PORT=<ADMIN_SSH_PORT>  ADMIN_USER=<localuser>-admin
#                 APP_DIR=/data/BioAgent/app  ROOT_DIR=/data/BioAgent
#                 SVC_USER=aiscientist  CONSOLE_PORT=8800
#                 BIND_HOST=<empty>   internal node IP the console must bind so the cluster
#                     ingress reaches it — set <GATEWAY_BIND_IP> for the PUBLIC prod (else start.sh
#                     rebinds 127.0.0.1 and drops the public site). Empty = localhost dev.
#                 HEALTH_HOST=<BIND_HOST or 127.0.0.1>  host the health check curls on the server.
#
set -euo pipefail

# Per-user config (gitignored) — the SAME .deploy.env scripts/sync_deploy.sh uses, so shared
# knobs (notably BIND_HOST for the public bind) live in ONE place. Sourced before the defaults
# below so its values win via the ${VAR:-default}s. Skipped silently if absent (dev / no config).
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
[ -f "$REPO_ROOT/.deploy.env" ] && . "$REPO_ROOT/.deploy.env"

HOST="${HOST:-<GATEWAY_HOST>}"
PORT="${PORT:-}"
APP_DIR="${APP_DIR:-/data/BioAgent/app}"
ROOT_DIR="${ROOT_DIR:-/data/BioAgent}"
# Service account migrated bioagent -> aiscientist when the public domain went live (2026-07-01).
SVC_USER="${SVC_USER:-aiscientist}"
CONSOLE_PORT="${CONSOLE_PORT:-8800}"
# See the header: BIND_HOST = the internal node IP the app must bind for the ingress; empty = dev.
BIND_HOST="${BIND_HOST:-}"
HEALTH_HOST="${HEALTH_HOST:-${BIND_HOST:-127.0.0.1}}"
HOST_ENV=""
[ -n "$BIND_HOST" ] && HOST_ENV="BIOAGENT_HOST=${BIND_HOST} "

DELETE="--delete"; ALLOW_DIRTY=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-delete) DELETE="" ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac; shift
done

cd "$REPO_ROOT"   # REPO_ROOT computed above (before sourcing .deploy.env)
say()  { printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- who + what -------------------------------------------------------------
default_admin="$(whoami)-admin"
read -r -p "Your eyeserver ADMIN account (in the sudo group) [${default_admin}]: " ADMIN_USER
ADMIN_USER="${ADMIN_USER:-${ADMIN_USER:-$default_admin}}"
[ -n "$ADMIN_USER" ] || ADMIN_USER="$default_admin"
TARGET="${ADMIN_USER}@${HOST}"

if [ "$ALLOW_DIRTY" -eq 0 ] && [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  die "local tree is dirty — commit/stash first or pass --allow-dirty."
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
STAGE="/tmp/bioagent-deploy-${ADMIN_USER}"
say "deploy ${BRANCH}@${SHA}  ->  ${TARGET}:${APP_DIR}  (as ${SVC_USER}; delete=${DELETE:-off})"

# --- shared excludes (protect server-only state from --delete) --------------
# Patterns with globs (*.db ...) must be passed quoted to BOTH rsyncs so neither the local
# nor the REMOTE shell glob-expands them before rsync sees them.
PATTERNS=(
  '.git/' '.env' '*.db' '*.sqlite*' '.venv/' 'venv/' 'env/' '__pycache__/' '*.pyc'
  'runs/' '.adaptive_kg/' 'node_modules/' '*.sif' 'console.log' '*.egg-info/'
  '.claude/settings.local.json'
)
EXC=(); for p in "${PATTERNS[@]}"; do EXC+=(--exclude "$p"); done   # local rsync (array)
EXC_STR=""; for p in "${PATTERNS[@]}"; do EXC_STR+=" --exclude '$p'"; done   # remote rsync (quoted)

# --- 1. ONE ssh master connection (password #1: your SSH login) -------------
CTL="$HOME/.ssh/cm-bioagent-$$"
cleanup() { ssh -O exit -o ControlPath="$CTL" -p "$PORT" "$TARGET" 2>/dev/null || true; }
trap cleanup EXIT
say "[1/2] opening SSH connection — enter your ${ADMIN_USER} SSH password if asked ..."
ssh -o ControlMaster=yes -o ControlPath="$CTL" -o ControlPersist=600 \
    -o ConnectTimeout=20 -p "$PORT" "$TARGET" 'echo connected as $(whoami)' \
  || die "SSH connection/auth failed for ${TARGET}:${PORT}."

SSHM=(ssh -o ControlPath="$CTL" -p "$PORT")

# --- 2. rsync local -> staging you own (no sudo) ----------------------------
say "syncing your local tree -> ${STAGE} (staging) ..."
rsync -az --delete "${EXC[@]}" -e "ssh -o ControlPath=$CTL -p $PORT" \
  ./ "${TARGET}:${STAGE}/" || die "staging rsync failed."

# --- 3. ONE privileged step as bioagent (password #2: your sudo) ------------
# Written to a file on the server, then run under `sudo -u bioagent` with a TTY so the
# sudo prompt works. It mirrors staging -> app (re-applying excludes so app's .env/DB/.git
# survive --delete), reinstalls, and restarts the console.
REMOTE_STEP="$(cat <<REMOTE
set -euo pipefail
rsync -a ${DELETE} ${EXC_STR} "${STAGE}/" "${APP_DIR}/"
cd "${APP_DIR}"
"${ROOT_DIR}/env/bin/pip" install -e . -q || echo "[deploy] pip install warned (continuing)"
# Record what was deployed so local vs server can be compared.
echo "${SHA} ${BRANCH} $(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${APP_DIR}/.deployed_sha"
# start.sh stops the old worker and starts a fresh one (prints a build fingerprint).
# ${HOST_ENV} sets BIOAGENT_HOST (the internal node IP) so the restart binds where the ingress
# expects; empty for a localhost dev deploy. Interpolated locally when this heredoc is built.
${HOST_ENV}setsid ./start.sh </dev/null >>"${ROOT_DIR}/console.log" 2>&1 &
sleep 1
echo "[deploy] restarted as \$(whoami)"
REMOTE
)"
say "writing the privileged step to the server ..."
printf '%s\n' "$REMOTE_STEP" | "${SSHM[@]}" "$TARGET" "cat > ${STAGE}/__deploy_step.sh"

say "[2/2] applying as ${SVC_USER} — enter your ${ADMIN_USER} sudo password if asked ..."
ssh -t -o ControlPath="$CTL" -p "$PORT" "$TARGET" \
  "sudo -u ${SVC_USER} bash ${STAGE}/__deploy_step.sh" \
  || die "privileged apply/restart failed (sudo password? sudo -u ${SVC_USER} allowed?)."

# --- 4. health check --------------------------------------------------------
say "checking the console on ${HEALTH_HOST}:${CONSOLE_PORT} ..."
ok=0
for _ in $(seq 1 20); do
  code="$("${SSHM[@]}" "$TARGET" "curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://${HEALTH_HOST}:${CONSOLE_PORT}/ 2>/dev/null || true")"
  case "$code" in 200|401|302) ok=1; break ;; esac
  sleep 2
done
"${SSHM[@]}" "$TARGET" "tail -n 20 ${ROOT_DIR}/console.log" 2>/dev/null || true
if [ "$ok" -eq 1 ]; then
  if [ -n "$BIND_HOST" ]; then
    say "✅ console is up (HTTP ${code}). ${BRANCH}@${SHA} is live (bound ${BIND_HOST}:${CONSOLE_PORT}) — https://<PUBLIC_HOSTNAME>/"
  else
    say "✅ console is up (HTTP ${code}). ${BRANCH}@${SHA} is live — reach it via your SSH tunnel."
  fi
else
  die "console did not answer on ${HEALTH_HOST}:${CONSOLE_PORT} (HTTP '${code:-none}') — see the log above."
fi
