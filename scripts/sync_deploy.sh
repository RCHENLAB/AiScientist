#!/usr/bin/env bash
#
# sync_deploy.sh — push the LOCAL working tree to the eyeserver and restart the console.
#
# WHY rsync (not `git pull` on the server): the server's git remote is the PRIVATE GitHub
# repo with no stored credentials, so it CANNOT fetch. rsync from a local checkout that
# already has the code is the reliable path — and it literally syncs "your local state".
#
# WHAT it does:
#   1. (safety) refuse to deploy a dirty local tree unless --allow-dirty.
#   2. rsync the repo -> $APP_DIR on the server (excludes .git, .env, the DB, venv, runs/,
#      caches, large artifacts — so server-only state is never clobbered).
#   3. reinstall the package into the server venv (idempotent; picks up new modules/deps).
#   4. restart the console THROUGH systemd (`sudo systemctl restart bioagent`) — stopping the
#      old instance and killing any stray non-systemd orphan first, so exactly ONE instance
#      owns the port. (The old path detached a start.sh orphan that fought the systemd unit
#      for :8800 and wedged it in a 1000s-deep crash-restart loop — never do that again.)
#   5. health-check the console port and tail the journal.
#
# SUDO vs no-sudo — two paths, both supported:
#   * SUDO set (DEFAULT, you connect as your OWN account): we CANNOT run `sudo` over the
#     rsync transport (it's binary — there is no TTY for a password prompt; that is the
#     classic "sudo: a terminal is required to read the password" failure). So instead we
#     rsync to a world-readable STAGING dir AS YOU (no sudo), then run the privileged
#     mirror -> pip install -> restart in ONE `ssh -t` sudo session (a TTY IS allocated, so
#     sudo prompts you exactly ONCE). No passwordless sudo and no server config required.
#   * SUDO="" (you connect AS the service account, e.g. an `eyeserver-aiscientist` alias): we
#     rsync straight into $APP_DIR and run the steps directly — no sudo, no staging.
#
# PREREQUISITES (the operator running this needs):
#   - a clean local checkout of the branch you want live, with rsync + ssh installed;
#   - SSH access to the server (DEPLOY_SSH). For the SUDO path you also need to be able to
#     `sudo -u <svc>` on the server (you WILL be prompted for your sudo password, once, on
#     your terminal — that is expected and interactive). For the SUDO="" path you instead
#     log in directly as the service account.
#
# USAGE:
#   scripts/sync_deploy.sh [options]
#
#   --dry-run        show what rsync WOULD copy + the restart plan; change nothing.
#   --no-restart     sync + reinstall only; do not restart the service.
#   --no-install     skip the `pip install -e` step (use when only static/frontend changed).
#   --allow-dirty    deploy even if the local git tree has uncommitted changes.
#   --delete         let rsync DELETE remote $APP_DIR files not present locally (off by
#                    default — safer; the server-only excludes below are always protected).
#   -h | --help      this help.
#
# Put your settings in .deploy.env (gitignored, sourced automatically) so you don't retype
# them — e.g. `ADMIN_SSH=eyeserver-admin`.
#
# ENV KNOBS (defaults in []):
#   DEPLOY_SSH  [eyeserver]            ssh target for the bulk rsync (host or alias)
#   ADMIN_SSH   [=DEPLOY_SSH]          ssh target for the PRIVILEGED (sudo) steps. Set this
#                                      to an account that can `sudo -u aiscientist` when your
#                                      normal login can't — here, eyeserver-admin (<admin-ucinetid>,
#                                      passwordless sudo) vs eyeserver (<ucinetid>, no sudo).
#   APP_DIR     [/data/BioAgent/app]   remote app dir (the git working tree)
#   ROOT_DIR    [/data/BioAgent]       remote base (holds env/ and console.log)
#   SVC_USER    [aiscientist]          service account that owns + runs the console
#   PORT        [8800]                 console port for the health check
#   SUDO        [sudo -u aiscientist]  how to act as the service account; set SUDO=""
#                                      if you ARE already the service account on the server.
#   STAGE_DIR   [/tmp/bioagent-deploy-$USER]   world-readable rsync staging dir on the
#                                      server (SUDO path only; reused across deploys).
#   BIND_HOST   [empty]               internal node IP the console must bind so the cluster
#                                      ingress (Envoy) can reach it — set to <GATEWAY_BIND_IP> in
#                                      .deploy.env for the PUBLIC prod. Empty = localhost dev
#                                      (start.sh keeps 127.0.0.1). Passed to the restart as
#                                      BIOAGENT_HOST; without it a restart would rebind loopback
#                                      and drop the public site.
#   HEALTH_HOST [=BIND_HOST or 127.0.0.1]  host the post-deploy health check curls on the server.
#
# EXAMPLES:
#   # preview only (no change)
#   scripts/sync_deploy.sh --dry-run
#   # full deploy of the current branch, OVERWRITING the server's tree exactly (recommended
#   # when the server tree is stale/divergent — also removes files you deleted locally):
#   scripts/sync_deploy.sh --delete --dry-run     # review first
#   scripts/sync_deploy.sh --delete               # then apply
#
# TEAM SETUP (so coworkers can deploy/debug without your account or sudo):
#   One-time, by someone with sudo — add each coworker's PUBLIC key to the service account:
#     cat ~/.ssh/id_ed25519.pub | ssh eyeserver 'sudo tee -a /home/aiscientist/.ssh/authorized_keys'
#   Each coworker adds an ssh alias in ~/.ssh/config:
#     Host eyeserver-aiscientist
#         HostName <server host/IP>
#         User aiscientist
#   Then EVERYONE deploys with the same command (own key, acts as the service account):
#     DEPLOY_SSH=eyeserver-aiscientist SUDO="" scripts/sync_deploy.sh --delete
#
#   Whoever has the admin account just sets ADMIN_SSH to it (in .deploy.env): the bulk rsync
#   uses their normal login, the sudo step uses the admin login.
#
#   THIS DEPLOYMENT: bulk rsync over `eyeserver` (<ucinetid>), privileged step over
#   `eyeserver-admin` (<admin-ucinetid>, passwordless sudo) — set `ADMIN_SSH=eyeserver-admin`
#   in .deploy.env. The normal <ucinetid> account has no usable sudo, so it CANNOT be the
#   privileged account.
#
# NOTE: this syncs CODE + restarts. It does NOT run DB migrations. The User/Dataset/Run/
# Conversation/Message schema is created by the app on boot (db.init_db); if you changed
# models, migrate the Postgres schema separately before/after.
#
set -euo pipefail

# Per-user config (gitignored) — set DEPLOY_SSH / ADMIN_SSH / APP_DIR etc. here so you
# don't retype them. Sourced before defaults so its values win via the ${VAR:-default}s.
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=/dev/null
[ -f "$REPO_ROOT/.deploy.env" ] && . "$REPO_ROOT/.deploy.env"

DEPLOY_SSH="${DEPLOY_SSH:-eyeserver}"
# ADMIN_SSH carries the PRIVILEGED (sudo) steps. It must log in as an account that can
# `sudo -u ${SVC_USER}` — which is often NOT your normal login (e.g. here the normal
# `eyeserver`/<ucinetid> account has no sudo; the `eyeserver-admin`/<admin-ucinetid> account
# does, passwordless). The bulk rsync still goes over DEPLOY_SSH (no sudo needed); only
# the short mirror+install+restart uses ADMIN_SSH. Defaults to DEPLOY_SSH.
ADMIN_SSH="${ADMIN_SSH:-$DEPLOY_SSH}"
APP_DIR="${APP_DIR:-/data/BioAgent/app}"
ROOT_DIR="${ROOT_DIR:-/data/BioAgent}"
SVC_USER="${SVC_USER:-aiscientist}"
PORT="${PORT:-8800}"
SUDO="${SUDO-sudo -u ${SVC_USER}}"
# The console runs under systemd (deploy/systemd/bioagent.service) as ${SVC_USER}. The
# SUDO path restarts THROUGH systemd (sudo systemctl) — never a detached start.sh, which
# would spawn an orphan that fights the systemd-managed instance for the port and wedge it
# in a crash-restart loop. SERVICE names that unit.
SERVICE="${BIOAGENT_SERVICE:-bioagent}"
DEPLOY_STAMP="$(date -u +%FT%TZ 2>/dev/null || echo unknown)"
STAGE_DIR="${STAGE_DIR:-/tmp/bioagent-deploy-${USER:-$(id -un)}}"
# Public-domain deploy: the console now binds a SPECIFIC internal node IP (the Envoy Gateway
# terminates TLS on :443 and routes there; 127.0.0.1 and the public NIC are refused). Set
# BIND_HOST to that IP (e.g. <GATEWAY_BIND_IP> in .deploy.env) so the restart binds where the
# ingress expects — otherwise start.sh's 127.0.0.1 default would rebind to loopback and drop
# the public site. Empty = localhost dev deploy (unchanged: start.sh keeps 127.0.0.1).
# HEALTH_HOST is where the post-deploy health check curls; defaults to BIND_HOST, else 127.0.0.1.
BIND_HOST="${BIND_HOST:-}"
# Default the health target to whatever the service is ACTUALLY listening on, not 127.0.0.1:
# this deployment binds one routable IP (systemd passes --host), so a loopback probe is refused
# and the deploy reports "health check failed" for a console that is serving fine. Only fall back
# to loopback when nothing is bound yet (first install).
HEALTH_HOST="${HEALTH_HOST:-${BIND_HOST:-}}"
if [ -z "$HEALTH_HOST" ]; then
  HEALTH_HOST="$(ssh "$DEPLOY_SSH" "ss -ltn 2>/dev/null | awk '/:${PORT} /{split(\$4,a,\":\"); print a[1]; exit}'" 2>/dev/null || true)"
  [ -z "$HEALTH_HOST" ] || [ "$HEALTH_HOST" = "0.0.0.0" ] || [ "$HEALTH_HOST" = "*" ] && HEALTH_HOST=127.0.0.1
fi
# Only export BIOAGENT_HOST into the restart when BIND_HOST is set, so an unset BIND_HOST never
# overrides a bind the server already has (never silently forces loopback in prod).
HOST_ENV=""
[ -n "$BIND_HOST" ] && HOST_ENV="BIOAGENT_HOST=${BIND_HOST} "

DRY_RUN=0; DO_RESTART=1; DO_INSTALL=1; ALLOW_DIRTY=0; RSYNC_DELETE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-restart) DO_RESTART=0 ;;
    --no-install) DO_INSTALL=0 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --delete) RSYNC_DELETE="--delete" ;;
    -h|--help) sed -n '2,92p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

cd "$REPO_ROOT"

say() { printf '\033[1;36m[sync-deploy]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[sync-deploy] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# --- 1. safety: clean tree -------------------------------------------------
if [ "$ALLOW_DIRTY" -eq 0 ] && [ -n "$(git status --porcelain 2>/dev/null)" ]; then
  die "local tree is dirty — commit/stash first, or pass --allow-dirty. ($(git status --short | wc -l | tr -d ' ') changes)"
fi
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"      # short, for human-facing lines
FULL_SHA="$(git rev-parse HEAD 2>/dev/null || echo "$SHA")"       # full, for the .deployed_sha marker (provenance git_sha)
say "deploying ${BRANCH}@${SHA}  ->  ${SVC_USER}@${DEPLOY_SSH}:${APP_DIR}"

# --- 2. rsync code (never the server's .env / DB / venv / runs) -------------
# Excludes keep server-only state intact — honored both on the wire AND, with --delete, as
# deletion protection so .env/DB/venv/runs on the server are never removed.
EXCLUDE_PATTERNS=(
  'output/' 'work/' 'report-hero.jpeg' 'report-loop.jpeg' 'report-loop3.jpeg' 'Weixin Image_20260705170510_101_39.png'
  'tmp/' '.idea/' 'img.png' '_handoff_tmp.md'
  '.git/' '.env' '*.db' '*.sqlite*' '.venv/' 'venv/' 'env/'
  '__pycache__/' '*.pyc' 'runs/' '.adaptive_kg/' 'node_modules/'
  # '.claude/worktrees/' holds full nested worktree checkouts (a per-feature copy of the whole repo);
  # without this they get rsynced into the prod app dir — bloating every deploy and, absent --delete,
  # lingering forever after a worktree is removed. The app never loads from those nested copies.
  '*.sif' '.claude/settings.local.json' '.claude/worktrees/' 'console.log' '*.egg-info/'
  # sample_data/ holds test/demo VCFs + case notes + run scripts (also gitignored). They are for
  # local verification, never served — keep them out of the prod app dir.
  'sample_data/'
)
RSYNC_EXCLUDES=()
for _p in "${EXCLUDE_PATTERNS[@]}"; do RSYNC_EXCLUDES+=(--exclude "$_p"); done
RSYNC_BASE=(-az --human-readable --itemize-changes "${RSYNC_EXCLUDES[@]}")
[ "$DRY_RUN" -eq 1 ] && RSYNC_BASE+=(--dry-run)

if [ -z "$SUDO" ]; then
  # ---- direct path: we ARE the service account; rsync straight into $APP_DIR ----------
  say "rsync ${DRY_RUN:+(dry-run) }-> ${APP_DIR}/ (no sudo) ..."
  rsync "${RSYNC_BASE[@]}" $RSYNC_DELETE ./ "${DEPLOY_SSH}:${APP_DIR}/"

  if [ "$DRY_RUN" -eq 1 ]; then
    say "dry-run only — no install, no restart. Restart plan: start.sh in ${APP_DIR}."
    exit 0
  fi
  if [ "$DO_INSTALL" -eq 1 ]; then
    say "pip install -e . into the server venv ..."
    # PIP_CACHE_DIR: the service account may have no home, so pip can't use ~/.cache/pip — cache
    # into a dir under ${ROOT_DIR} instead (best-effort mkdir; pip just skips the cache if unwritable).
    # shellcheck disable=SC2029
    ssh "$DEPLOY_SSH" "bash -lc 'cd ${APP_DIR} && mkdir -p ${ROOT_DIR}/.pip-cache 2>/dev/null; PIP_CACHE_DIR=${ROOT_DIR}/.pip-cache ${ROOT_DIR}/env/bin/pip install -e . -q'" \
      || die "pip install failed on the server (see output above)."
  fi
  if [ "$DO_RESTART" -eq 1 ]; then
    # WARNING: this path detaches a start.sh worker directly. Use it ONLY on a host with no
    # systemd bioagent.service — on the prod host that unit is enabled, and a start.sh orphan
    # will fight it for the port. On prod use the default (sudo) path, which restarts via systemd.
    say "restarting the console (start.sh — no-systemd/dev path) ..."
    ssh "$DEPLOY_SSH" \
      "bash -lc 'cd ${APP_DIR} && ${HOST_ENV}setsid ./start.sh </dev/null >>${ROOT_DIR}/console.log 2>&1 & sleep 1; echo started'" \
      || die "restart command failed."
  fi
else
  # ---- sudo path: stage as us (no sudo), then ONE ssh -t sudo session for the rest -----
  # The privileged rsync/install/restart can't read a password over a non-TTY ssh, so we
  # split the transfer (as you, into a world-readable staging dir) from the privileged
  # mirror (under `ssh -t`, which DOES give sudo a TTY to prompt on — exactly once).
  STAGE_EXCLUDES="${STAGE_DIR}.excludes"
  say "rsync ${DRY_RUN:+(dry-run) }-> staging ${DEPLOY_SSH}:${STAGE_DIR}/ (as ${USER:-you}, no sudo) ..."
  # Staging is a throwaway exact mirror of the local wanted-set, so always --delete it
  # (independent of the user's --delete, which governs $APP_DIR below).
  rsync "${RSYNC_BASE[@]}" --delete ./ "${DEPLOY_SSH}:${STAGE_DIR}/"

  if [ "$DRY_RUN" -eq 1 ]; then
    say "dry-run only — privileged mirror to ${APP_DIR} skipped."
    say "Real run does, in ONE 'ssh -t ${ADMIN_SSH}' sudo session:"
    say "  sudo -u ${SVC_USER} rsync ${RSYNC_DELETE:+--delete }staging -> ${APP_DIR}"
    [ "$DO_INSTALL" -eq 1 ] && say "  sudo -u ${SVC_USER} pip install -e ${APP_DIR}"
    [ "$DO_RESTART" -eq 1 ] && say "  sudo systemctl restart ${SERVICE}  (stops old + kills any orphan first)"
    exit 0
  fi

  # Hand the protect-list to the privileged rsync, and make staging readable by ${SVC_USER}
  # (the privileged step may run under a DIFFERENT account than the staging rsync).
  printf '%s\n' "${EXCLUDE_PATTERNS[@]}" | ssh "$DEPLOY_SSH" "cat > ${STAGE_EXCLUDES}"
  ssh "$DEPLOY_SSH" "chmod -R a+rX ${STAGE_DIR} && chmod a+r ${STAGE_EXCLUDES}" \
    || die "could not make the staging dir readable by ${SVC_USER}."

  # Assemble the privileged steps as ONE foreground chain (set -e aborts on any failure).
  # Each step self-sudos: file ops as ${SVC_USER} (so the tree stays owned by the service
  # account), the RESTART as root through systemd. We DON'T wrap the whole chain in a single
  # `sudo -u ${SVC_USER}` shell anymore — that account can't run systemctl, which is exactly
  # why the old start.sh path leaked an orphan and left systemd crash-looping on the port.
  REMOTE_STEPS="set -e"
  REMOTE_STEPS="${REMOTE_STEPS}; sudo -u ${SVC_USER} rsync -a ${RSYNC_DELETE} --exclude-from=${STAGE_EXCLUDES} ${STAGE_DIR}/ ${APP_DIR}/"
  # pip runs as ${SVC_USER} with:
  #   env -C ${APP_DIR} : CWD is a dir ${SVC_USER} CAN read. Without this, pip runs in the sudo
  #                caller's HOME (e.g. /home/<admin-ucinetid>, mode 700) and its editable-package
  #                enumeration does os.stat() on RELATIVE paths there -> EACCES, aborting the whole
  #                deploy (a stale `__editable__.biomni*` finder in the venv triggers exactly this).
  #   PIP_CACHE_DIR=${PIP_CACHE} : ${SVC_USER} is a service account with NO home dir, so pip can't
  #                create its default ~/.cache/pip and warns "cache not writable / disabled" on every
  #                deploy. Point the cache at a ${SVC_USER}-owned dir under ${ROOT_DIR} instead — we
  #                mkdir+chown it as root first, so it exists and is writable regardless of the home.
  #                (-H is dropped: it only pointed HOME at the non-existent /home/${SVC_USER}.)
  # Neither chown-ing the venv nor -H fixes the CWD problem — only a readable CWD does.
  # (--no-install skips this entirely; an editable install already imports new modules from src/.)
  PIP_CACHE="${ROOT_DIR}/.pip-cache"
  if [ "$DO_INSTALL" -eq 1 ]; then
    # As ${SVC_USER}, NOT root: ${ROOT_DIR} is group-writable + setgid and already owned by the
    # service account, so root buys nothing here — it only forced a password prompt for a step
    # that a NOPASSWD grant on ${SVC_USER} already covers.
    REMOTE_STEPS="${REMOTE_STEPS}; sudo -u ${SVC_USER} mkdir -p ${PIP_CACHE}"
    REMOTE_STEPS="${REMOTE_STEPS}; sudo -u ${SVC_USER} env -C ${APP_DIR} PIP_CACHE_DIR=${PIP_CACHE} ${ROOT_DIR}/env/bin/pip install -e . -q"
  fi
  # Truthful deployed-SHA marker (the server's own .git is a stale, rsync-frozen relic — never
  # trust `git -C ${APP_DIR}`; trust this).
  # The marker records what is INSTALLED. With --no-restart the running process is still the old
  # code, so say so in the marker itself rather than letting a later reader believe the new sha is
  # live — that gap is exactly how a "deployed but not actually running" state goes unnoticed.
  _sha_note=""; [ "$DO_RESTART" -eq 0 ] && _sha_note=" (installed, NOT yet restarted)"
  REMOTE_STEPS="${REMOTE_STEPS}; echo '${FULL_SHA} ${BRANCH} ${DEPLOY_STAMP}${_sha_note}' | sudo -u ${SVC_USER} tee ${APP_DIR}/.deployed_sha >/dev/null"
  # Restart THROUGH systemd. stop (disarms auto-restart) -> kill any non-systemd orphan
  # squatting the port (self-heals a prior start.sh deploy) -> start a single clean instance.
  # pkill pattern uses a `[b]` char-class so the regex matches the real `bioagent.gateway`
  # process but NOT this deploy command's own line (which literally contains the pattern) —
  # without it, `pkill -f 'bioagent.gateway'` kills the parent shell running the deploy
  # (self-suicide) and `systemctl start` never runs.
  # Warm the root credential ONCE, with a VISIBLE prompt, before any redirected step runs.
  # This is not cosmetic. `sudo systemctl stop … 2>/dev/null` sent sudo's OWN password prompt to
  # /dev/null: the operator saw a silent hang, typed into a prompt that was never displayed, and
  # `|| true` then swallowed the auth failure — so the next, un-redirected sudo reported
  # "Sorry, try again" for a password that was correct, and the restart was skipped without a
  # word. Prompting once up front means every later sudo hits the cached timestamp instead.
  if [ "$DO_RESTART" -eq 1 ]; then
    # FULLY-QUALIFIED unit name. sudo matches a Cmnd_Alias on the exact argv, so a NOPASSWD grant
    # written for `systemctl restart bioagent.service` does NOT match `… restart bioagent`; the
    # call silently falls through to the broad (ALL:ALL) rule and asks for a password. Always
    # send the same spelling the grant uses.
    _unit="${SERVICE%.service}.service"
    # NEVER redirect a sudo that may prompt: its password prompt goes to stderr, and hiding it is
    # what made a correct password look wrong (see the 2026-08-10 handoff entry).
    REMOTE_STEPS="${REMOTE_STEPS}; sudo systemctl stop ${_unit} || true"
    # Orphan sweep for the legacy detached-start path. Root, and deliberately NOT part of the
    # narrow service grant — so try it non-interactively and skip it rather than prompt.
    REMOTE_STEPS="${REMOTE_STEPS}; sudo -n pkill -f '[b]ioagent[.]gateway' 2>/dev/null || true"
    REMOTE_STEPS="${REMOTE_STEPS}; sleep 1; sudo systemctl start ${_unit}; sleep 2; systemctl is-active ${_unit}"
  fi

  # Privileged step runs over ADMIN_SSH (the account that can sudo). -t allocates a TTY so
  # password-sudo can prompt ONCE; with passwordless sudo (e.g. <admin-ucinetid>) it just runs.
  _il=""; [ "$DO_INSTALL" -eq 1 ] && _il=" + install"
  _rl=""; [ "$DO_RESTART" -eq 1 ] && _rl=" + systemd restart"
  say "privileged mirror${_il}${_rl} as ${SVC_USER} via ${ADMIN_SSH} ..."
  # shellcheck disable=SC2029
  ssh -t "$ADMIN_SSH" "bash -lc \"${REMOTE_STEPS}\"" \
    || die "privileged deploy step failed (see output above)."
fi

# --- 5. health check -------------------------------------------------------
say "waiting for the console on ${HEALTH_HOST}:${PORT} ..."
ok=0
for _ in $(seq 1 20); do
  code="$(ssh "$DEPLOY_SSH" "curl -s -o /dev/null -w '%{http_code}' --max-time 4 http://${HEALTH_HOST}:${PORT}/ 2>/dev/null || true")"
  if [ "$code" = "200" ] || [ "$code" = "401" ] || [ "$code" = "302" ]; then ok=1; break; fi
  sleep 2
done
if [ "$ok" -eq 1 ]; then
  say "✅ console is up (HTTP ${code}). Tail of the log:"
else
  say "⚠️  console did not answer on ${HEALTH_HOST}:${PORT} yet (HTTP '${code:-none}'). Recent log:"
fi
# Under systemd the app logs to the JOURNAL, not console.log. Reading it needs root; with
# non-interactive sudo we skip rather than hang.
#
# The console.log fallback is deliberately gated on the file being FRESH. It is written only by
# the legacy detached-start.sh path, so on a systemd deployment it is a relic — here it had been
# frozen since 2026-07-02, and its last lines are "Shutting down". Dumping that under the banner
# "Recent log:" reads as PRODUCTION JUST WENT DOWN. It cost a real false alarm. A diagnostic that
# can lie about the present is worse than no diagnostic, so stale output is refused by name.
if ! ssh "$ADMIN_SSH" "sudo -n journalctl -u ${SERVICE} -n 25 --no-pager 2>/dev/null"; then
  _log_age="$(ssh "$DEPLOY_SSH" "test -f ${ROOT_DIR}/console.log && echo \$(( ( \$(date +%s) - \$(stat -c %Y ${ROOT_DIR}/console.log) ) / 60 ))" 2>/dev/null || echo "")"
  if [ -n "$_log_age" ] && [ "$_log_age" -lt 60 ]; then
    ssh "$DEPLOY_SSH" "tail -n 25 ${ROOT_DIR}/console.log 2>/dev/null" || true
  else
    say "journal needs root (no passwordless sudo) and ${ROOT_DIR}/console.log is${_log_age:+ ${_log_age} min} stale — NOT showing it."
    say "For the real log:  ssh ${ADMIN_SSH} sudo journalctl -u ${SERVICE} -n 50"
  fi
fi
[ "$ok" -eq 1 ] || die "health check failed — check: ssh ${ADMIN_SSH} sudo journalctl -u ${SERVICE} -n 50"
# Public prod (BIND_HOST set): the site is reachable at its public HTTPS domain via the cluster
# ingress. Localhost dev (no BIND_HOST): reach it through your SSH tunnel to ${HEALTH_HOST}:${PORT}.
if [ -n "$BIND_HOST" ]; then
  say "done: ${BRANCH}@${SHA} is live (bound ${BIND_HOST}:${PORT}) — https://<PUBLIC_HOSTNAME>/"
else
  say "done: ${BRANCH}@${SHA} is live on ${HEALTH_HOST}:${PORT} (reach it via your SSH tunnel)."
fi
