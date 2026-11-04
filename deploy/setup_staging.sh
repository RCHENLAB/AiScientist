#!/usr/bin/env bash
#
# Set up the staging site on the Eye Server, once: a second AiScientist site that installs every
# pre-release by itself, so a release is tried before the production site gets it (deploy/README.md,
# "Staging site"). Run ON the server as root, from the package of the release it starts with (one
# whose deploy_release.sh knows AISCIENTIST_CHANNEL):
#
#   cd /tmp && curl -fsSLO https://github.com/RCHENLAB/AiScientist/releases/download/<tag>/aiscientist-<tag>.tar.gz
#   curl -fsSLO https://github.com/RCHENLAB/AiScientist/releases/download/<tag>/SHA256SUMS
#   sha256sum -c SHA256SUMS && tar xzf aiscientist-<tag>.tar.gz
#   sudo bash aiscientist-<tag>/deploy/setup_staging.sh --tag <tag>            # checks; changes nothing
#   sudo bash aiscientist-<tag>/deploy/setup_staging.sh --tag <tag> --apply    # sets it up and starts it
#   sudo bash aiscientist-<tag>/deploy/setup_staging.sh --remove               # stops it; keeps its data
#
# --apply, each step skipped when it is done already:
#   1. the service account aiscientist-staging, no login shell, home $ROOT/home;
#   2. $ROOT/: app/, config/ and state/ (0700), data/, logs/, skills/releases/, backups/env/, and the
#      gene sets copied from the production site's data/reference/genesets/;
#   3. a PostgreSQL role and database aiscientist_staging with a random password (as postgres);
#   4. config/.env (0600): the production site's settings (HPC3, Slurm, images, models), without
#      its credentials, its key service, its email, its shared model job and its folders; then this
#      site's own: the database, the staging folders, its own folder on HPC3 (<production shared
#      root>-staging, which must exist on HPC3), no CPU worker node, no self-registration (an admin
#      makes the accounts; the port is reachable by every account on the host); and
#      config/deploy.env: the prerelease channel, the unit, port 8801, no key service;
#   5. aiscientist-staging.service, aiscientist-staging-deploy.{service,timer} and the restart rule
#      /etc/sudoers.d/aiscientist-staging-restart;
#   6. <tag>, installed by deploy_release.sh as the service account (venv, migration, start,
#      /health), the skills release it pins, then the timer: from then on the site installs each new
#      pre-release within 10 minutes.
# Nothing of the production site changes: not its unit, root, database, accounts or key service.
# Its Slurm jobs are named with the prefix stg- (AISCIENTIST_SLURM_JOB_PREFIX), so neither site sees
# or cancels the other's, and one HPC3 account can be connected to both.
#
# --remove stops and removes the units and the restart rule. The account, $ROOT and the database
# stay; how to delete them is printed.
#
# For a rehearsal every system path is a setting: AISCIENTIST_STAGING_ROOT, SVC_USER, PORT,
# PROD_ROOT, SYSTEMD_DIR, SUDOERS_DIR, SYSTEMCTL, REQUIRE_ROOT=0, AS_SVC ("" = run as yourself),
# PSQL ("" = a SQLite file in state/ instead of PostgreSQL, for a rehearsal only),
# STAGING_HPC_SHARED_ROOT (default: the production site's AISCIENTIST_HPC_SHARED_ROOT + -staging),
# STAGING_SLURM_JOB_PREFIX (default: stg-).
set -euo pipefail

ROOT="${AISCIENTIST_STAGING_ROOT:-/data/aiscientist-staging}"
PROD_ROOT="${PROD_ROOT:-/data/aiscientist}"
SVC_USER="${SVC_USER:-aiscientist-staging}"
UNIT="${UNIT:-aiscientist-staging.service}"
PORT="${PORT:-8801}"
DB_NAME="${DB_NAME:-aiscientist_staging}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
REQUIRE_ROOT="${REQUIRE_ROOT:-1}"
AS_SVC="${AS_SVC-sudo -u $SVC_USER}"
PSQL="${PSQL-sudo -u postgres psql -v ON_ERROR_STOP=1 -X -q}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="$(cd "$HERE/.." && pwd)"
RULE="$SUDOERS_DIR/aiscientist-staging-restart"
UNITS=(aiscientist-staging.service aiscientist-staging-deploy.service aiscientist-staging-deploy.timer)

MODE=check TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift ;;
    --apply) MODE=apply ;;
    --remove) MODE=remove ;;
    -h|--help) sed -n 2,42p "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '\n==> [%s] %s\n' "$(date +%T)" "$*"; }
ok()   { printf '    ok    %s\n' "$*"; }
bad()  { printf '    FAIL  %s\n' "$*"; FAILED=$((FAILED + 1)); }
warn() { printf '    note  %s\n' "$*"; }
die()  { printf '\nABORT: %s\n' "$*" >&2; exit 1; }
as_svc() { (cd /tmp && $AS_SVC "$@"); }
deploy() { as_svc env AISCIENTIST_ROOT="$ROOT" bash "$PKG/deploy/deploy_release.sh" "$@"; }
FAILED=0
OWN=(-o "$SVC_USER" -g "$SVC_USER"); [ "$REQUIRE_ROOT" = 1 ] || OWN=()
# The Python each release's venv is made from: production's (config/deploy.env), so the lock fits.
prod_setting() { { grep -E "^$1=" "$PROD_ROOT/config/deploy.env" 2>/dev/null || true; } | tail -n 1 | cut -d= -f2- | tr -d '"'"'"; }
PY_SETTING="${AISCIENTIST_PYTHON:-$(prod_setting AISCIENTIST_PYTHON)}"
PROD_ENV="$PROD_ROOT/config/.env"
prod_env_value() { { grep -E "^AISCIENTIST_$1=" "$PROD_ENV" 2>/dev/null || true; } | tail -n 1 | cut -d= -f2- | tr -d '"'"'"; }
PROD_SHARED="$(prod_env_value HPC_SHARED_ROOT)"
HPC_ROOT="${STAGING_HPC_SHARED_ROOT:-${PROD_SHARED:+$PROD_SHARED-staging}}"
# Never empty or production's: the two sites find a user's Slurm jobs by name.
JOB_PREFIX="${STAGING_SLURM_JOB_PREFIX:-stg-}"
PROD_JOB_PREFIX="$(prod_env_value SLURM_JOB_PREFIX)"
# Settings of the production site that stay its own: this site's folders and database, the key
# service, email, the shared model job (two keepers would cancel each other's jobs), the CPU worker
# (the other site would take it over, then cancel it as stale), registration; and every credential.
SKIP='DATABASE_URL|STATE_DIR|DATA_ROOT|RESULTS_DIR|INDUCED_SKILLS_DIR|SKILLS_REGISTRY_RELEASES_ROOT|HPC_SHARED_ROOT|SLURM_JOB_PREFIX|KEY_SERVICE.*|SMTP_.*|SHARED_LLM.*|WORKER_NODE|PUBLIC_HTTPS|ALLOW_SELF_REGISTER|ADMIN_.*|SECRET_KEY.*|LLM_KEY_.*|LOCAL_TUNNEL_PORT|OLLAMA_LOCAL_PORT|LOCAL_CORPUS_ROOT|BIOMNI_DATA_PATH'
# A name that ends in a credential word (..._API_KEY, ..._TOKEN_FILE, ..._PASSWORD); not ..._MAX_TOKENS.
CREDENTIAL='(KEY|TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIALS?)(_FILE)?'

[ "$REQUIRE_ROOT" != 1 ] || [ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0 ...)"
[ "$MODE" = remove ] || [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+((a|b|rc)[0-9]+)?$ ]] \
  || die "--tag <the release to start with, e.g. v0.5.0rc1> is required"
case "$ROOT" in "$PROD_ROOT"|"$PROD_ROOT"/*) die "the staging root $ROOT is inside the production root $PROD_ROOT" ;; esac
case "$PROD_ROOT" in "$ROOT"/*) die "the production root $PROD_ROOT is inside the staging root $ROOT" ;; esac
[[ "$JOB_PREFIX" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$ ]] \
  || die "STAGING_SLURM_JOB_PREFIX=$JOB_PREFIX: up to 32 letters, digits, '.', '_' or '-', starting with a letter or digit"
[ "$JOB_PREFIX" != "$PROD_JOB_PREFIX" ] || die "the Slurm job-name prefix $JOB_PREFIX is the production site's"
[ "$UNIT" != aiscientist.service ] || die "the staging unit cannot be the production unit"
[ "$SVC_USER" != aiscientist ] || die "the staging site cannot run as the production service account"

preflight() {
  say "preflight (staging root $ROOT, unit $UNIT, port $PORT; the release package $PKG)"
  [ -f "$PKG/VERSION" ] && grep -qx "tag=$TAG" "$PKG/VERSION" && ok "the package is $TAG ($(sed -n 's/^sha=//p' "$PKG/VERSION" | cut -c1-12))" \
    || bad "$PKG/VERSION does not name $TAG: run this script from that release's package"
  grep -q 'AISCIENTIST_CHANNEL' "$PKG/deploy/deploy_release.sh" && ok "its deploy_release.sh follows a channel" \
    || bad "$TAG's deploy_release.sh has no AISCIENTIST_CHANNEL: start from a newer release"
  grep -qs 'AISCIENTIST_SLURM_JOB_PREFIX' "$PKG/src/aiscientist/gateway/settings.py" \
    && ok "its Slurm jobs are named $JOB_PREFIX* (the production site's: ${PROD_JOB_PREFIX:-no prefix})" \
    || bad "$TAG has no AISCIENTIST_SLURM_JOB_PREFIX: its Slurm jobs would take the production site's names"
  for f in "${UNITS[@]}"; do
    [ -f "$PKG/deploy/systemd/$f" ] || bad "the package has no deploy/systemd/$f"
  done
  if id -u "$SVC_USER" >/dev/null 2>&1; then ok "account $SVC_USER exists"; else warn "account $SVC_USER: created by --apply"; fi
  [ -d "$ROOT" ] && warn "$ROOT exists already: --apply keeps what is there" || ok "$ROOT: created by --apply"
  if [ -f "$SYSTEMD_DIR/$UNIT" ]; then warn "$UNIT is installed already"; else ok "$UNIT: installed by --apply"; fi
  if command -v ss >/dev/null && ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN; then
    [ -f "$SYSTEMD_DIR/$UNIT" ] && ok "port $PORT: in use by the staging site" || bad "port $PORT is in use by something else (PORT=...)"
  else
    ok "port $PORT is free"
  fi
  local py real; py="$(command -v "${PY_SETTING:-python3}" || true)"
  if [ -z "$py" ]; then
    bad "no ${PY_SETTING:-python3} on PATH"
  else
    ok "python: $py ($("$py" -V 2>&1)); the lock asks for $(sed -n 's/^# python: \([0-9.]*\).*/\1/p' "$PKG/requirements.lock" | head -n 1)"
    real="$(readlink -f "$py")"
    case "$real" in "$PROD_ROOT"/*) bad "$py is $real, inside $PROD_ROOT, which the staging units cannot see" ;; esac
  fi
  if compgen -G "$PROD_ROOT/data/reference/genesets/*.gmt" >/dev/null; then
    ok "gene sets to copy: $(ls "$PROD_ROOT"/data/reference/genesets/*.gmt | wc -l | tr -d ' ') file(s)"
  else
    warn "no gene sets in $PROD_ROOT/data/reference/genesets: enrichment on staging has no libraries"
  fi
  if [ -f "$PROD_ENV" ]; then
    ok "settings to copy from $PROD_ENV: $(copied_settings | wc -l | tr -d ' ') (credentials and the production site's own left out)"
  else
    bad "$PROD_ENV not found: the staging site copies the production site's HPC3 settings from it"
  fi
  if [ -n "$HPC_ROOT" ]; then
    ok "HPC3 folder of the staging site: $HPC_ROOT"
    warn "it must exist on HPC3 before the first connection: mkdir -m 2775 $HPC_ROOT; chgrp <the lab group> $HPC_ROOT"
  else
    bad "no AISCIENTIST_HPC_SHARED_ROOT in $PROD_ENV: set STAGING_HPC_SHARED_ROOT"
  fi
  if [ -n "$PSQL" ]; then
    $PSQL -tAc 'select 1' >/dev/null 2>&1 && ok "PostgreSQL answers as postgres" || bad "cannot run psql as postgres (PSQL=...)"
    local hba
    hba="$($PSQL -tAc "select auth_method from pg_hba_file_rules where type = 'host' and address = '127.0.0.1' limit 1" 2>/dev/null || true)"
    case "$hba" in
      md5|scram-sha-256|password) ok "loopback connections log in with a password ($hba)" ;;
      "") warn "no pg_hba rule for host 127.0.0.1 found: the staging database may refuse its login" ;;
      *) bad "loopback connections use $hba, not a password: the staging login would be refused" ;;
    esac
  else
    warn "PSQL is empty: SQLite in $ROOT/state (a rehearsal only)"
  fi
  curl -fsSI --max-time 20 -o /dev/null https://github.com/RCHENLAB/AiScientist/releases.atom \
    && ok "the releases feed answers" || warn "cannot reach https://github.com/RCHENLAB/AiScientist/releases.atom: the timer finds no pre-release"
  local prod
  prod="$(curl -fsS --max-time 5 "http://$(sed -n 's/^ExecStart=.*--host[ =]\([^ ]*\).*/\1/p' "$SYSTEMD_DIR/aiscientist.service.d/release-layout.conf" 2>/dev/null | tail -n 1):8800/health" 2>/dev/null || true)"
  [ -n "$prod" ] && ok "the production site answers: $prod (it is not touched)" || warn "the production site's /health did not answer"
  [ "$FAILED" -eq 0 ] || die "$FAILED check(s) failed; nothing was changed"
}

make_account() {
  say "account $SVC_USER"
  if id -u "$SVC_USER" >/dev/null 2>&1; then ok "exists"; return; fi
  useradd --system --user-group --home-dir "$ROOT/home" --no-create-home --shell /usr/sbin/nologin \
    --comment "AiScientist staging site" "$SVC_USER"
  ok "created (no login shell)"
}

make_dirs() {
  say "$ROOT"
  install -d -m 0750 ${OWN[@]+"${OWN[@]}"} "$ROOT"
  for d in app data data/analysis_results data/reference/genesets logs skills skills/releases home backups; do
    install -d -m 0750 ${OWN[@]+"${OWN[@]}"} "$ROOT/$d"
  done
  for d in config state backups/env; do
    install -d -m 0700 ${OWN[@]+"${OWN[@]}"} "$ROOT/$d"
  done
  if compgen -G "$PROD_ROOT/data/reference/genesets/*.gmt" >/dev/null; then
    install -m 0640 ${OWN[@]+"${OWN[@]}"} "$PROD_ROOT"/data/reference/genesets/*.gmt "$ROOT/data/reference/genesets/"
  fi
  ok "$(ls "$ROOT/data/reference/genesets" | wc -l | tr -d ' ') gene-set file(s); state/ and config/ are 0700"
}

# Prints the database URL; creates the role and the database when they are missing. The password
# is generated here and written only into config/.env: never printed, and given to psql on stdin, so
# no process list on this shared host shows it.
database_url() {
  if [ -z "$PSQL" ]; then echo "sqlite:///$ROOT/state/aiscientist.db"; return; fi
  local pw verb=create
  pw="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  [ "$($PSQL -tAc "select 1 from pg_roles where rolname = '$DB_NAME'")" != 1 ] || verb=alter
  printf "%s role %s with login password '%s';\n" "$verb" "$DB_NAME" "$pw" | $PSQL >/dev/null
  [ "$($PSQL -tAc "select 1 from pg_database where datname = '$DB_NAME'")" = 1 ] \
    || $PSQL -c "create database $DB_NAME owner $DB_NAME" >/dev/null
  echo "postgresql+psycopg://$DB_NAME:$pw@127.0.0.1:5432/$DB_NAME"
}

# The production site's settings that the staging site takes over as they are (no value in that file
# refers to another setting, so none changes meaning here).
copied_settings() {
  { grep -E '^AISCIENTIST_[A-Z0-9_]+=' "$PROD_ENV" 2>/dev/null || true; } \
    | { grep -vE "^AISCIENTIST_($SKIP)=" || true; } | { grep -vE "^AISCIENTIST_[A-Z0-9_]*$CREDENTIAL=" || true; }
}

write_config() {
  say "config/.env, config/deploy.env"
  if [ -f "$ROOT/config/.env" ]; then
    ok "config/.env exists: kept"
    grep -q '^AISCIENTIST_SLURM_JOB_PREFIX=.' "$ROOT/config/.env" \
      || warn "it has no AISCIENTIST_SLURM_JOB_PREFIX: add AISCIENTIST_SLURM_JOB_PREFIX=$JOB_PREFIX while no one uses the site"
  else
    local url; url="$(database_url)"
    umask 077
    {
      cat <<EOF
# The AiScientist staging site (deploy/setup_staging.sh). Only $SVC_USER reads this file.
# A separate site: its own database, accounts, folders and session secret (made in state/ on the
# first start). Settings: configs/aiscientist.example.env.
#
# Taken over from the production site's config/.env (HPC3, Slurm, images, models):
EOF
      copied_settings
      cat <<EOF

# This site's own:
AISCIENTIST_DATABASE_URL=$url
AISCIENTIST_STATE_DIR=$ROOT/state
AISCIENTIST_DATA_ROOT=$ROOT/data
AISCIENTIST_RESULTS_DIR=$ROOT/data/analysis_results
AISCIENTIST_INDUCED_SKILLS_DIR=$ROOT/state/induced_skills
AISCIENTIST_SKILLS_REGISTRY_RELEASES_ROOT=$ROOT/skills/releases
# Its own folder on HPC3: the two sites' cleanup sweeps keep only their own sessions.
AISCIENTIST_HPC_SHARED_ROOT=$HPC_ROOT
AISCIENTIST_SHARED_LLM=0
AISCIENTIST_WORKER_NODE=0
# Its own Slurm job names: a user connected to both sites keeps each site's jobs apart.
AISCIENTIST_SLURM_JOB_PREFIX=$JOB_PREFIX
# Reached over an SSH tunnel at http://localhost:$PORT, so the cookie is not marked Secure.
AISCIENTIST_PUBLIC_HTTPS=0
# Every account on this host can reach the port: an admin makes the accounts (Admin -> Users).
AISCIENTIST_ALLOW_SELF_REGISTER=0
EOF
    } > "$ROOT/config/.env.new"
    install -m 0600 ${OWN[@]+"${OWN[@]}"} "$ROOT/config/.env.new" "$ROOT/config/.env"
    rm -f "$ROOT/config/.env.new"
    umask 022
    ok "config/.env written (0600, $SVC_USER)"
  fi
  if [ -f "$ROOT/config/deploy.env" ]; then
    ok "config/deploy.env exists: kept"
  else
    cat > "$ROOT/config/deploy.env.new" <<EOF
# Settings of deploy/deploy_release.sh and deploy/deploy_skills.sh for the staging site
# (KEY=value; aiscientist-staging-deploy.timer reads them).
AISCIENTIST_REPO=RCHENLAB/AiScientist
AISCIENTIST_SKILLS_REPO=lijinbio/aiscientist-skills
# Pre-releases too: the newest release in the releases feed.
AISCIENTIST_CHANNEL=prerelease
AISCIENTIST_SERVICE=$UNIT
AISCIENTIST_PORT=$PORT
# Production's key service is not this site's.
AISCIENTIST_KEYS_SERVICE=none
AISCIENTIST_KEEP=3
${PY_SETTING:+AISCIENTIST_PYTHON=$PY_SETTING}
# Hold the site at one release instead of following the newest one:
#AISCIENTIST_APP_TAG=v0.5.0rc1
EOF
    install -m 0640 ${OWN[@]+"${OWN[@]}"} "$ROOT/config/deploy.env.new" "$ROOT/config/deploy.env"
    rm -f "$ROOT/config/deploy.env.new"
    ok "config/deploy.env written: channel prerelease, $UNIT, port $PORT"
  fi
}

install_units() {
  say "units and the restart rule $RULE"
  for f in "${UNITS[@]}"; do
    sed -e "s#/data/aiscientist-staging#$ROOT#g" -e "s#--port 8801#--port $PORT#" -e "s#:8801/#:$PORT/#" \
        -e "s#^User=aiscientist-staging#User=$SVC_USER#" -e "s#^Group=aiscientist-staging#Group=$SVC_USER#" \
        "$PKG/deploy/systemd/$f" > "$SYSTEMD_DIR/$f"
    chmod 0644 "$SYSTEMD_DIR/$f"
  done
  local systemctl_bin; systemctl_bin="$(command -v systemctl || echo /usr/bin/systemctl)"
  printf '# Installed by deploy/setup_staging.sh: deploy/deploy_release.sh restarts the staging site.\n%s ALL=(root) NOPASSWD: %s restart %s\n' \
    "$SVC_USER" "$systemctl_bin" "$UNIT" > "$RULE.new"
  if command -v visudo >/dev/null && [ "$REQUIRE_ROOT" = 1 ]; then visudo -cf "$RULE.new" >/dev/null || die "the sudoers rule does not parse"; fi
  chmod 0440 "$RULE.new" && mv -f "$RULE.new" "$RULE"
  $SYSTEMCTL daemon-reload
  $SYSTEMCTL enable "$UNIT" >/dev/null 2>&1 || true
  ok "${UNITS[*]}; $SVC_USER may restart $UNIT"
}

first_release() {
  say "install $TAG (venv from requirements.lock, migration, start, /health)"
  deploy "$TAG"
  say "the skills release it pins, and anything newer on the prerelease channel"
  deploy --auto
  $SYSTEMCTL enable --now aiscientist-staging-deploy.timer
  ok "aiscientist-staging-deploy.timer enabled: new pre-releases deploy themselves"
}

remove() {
  say "stop and remove the staging units and the restart rule"
  $SYSTEMCTL disable --now aiscientist-staging-deploy.timer >/dev/null 2>&1 || true
  $SYSTEMCTL disable --now "$UNIT" >/dev/null 2>&1 || true
  for f in "${UNITS[@]}"; do rm -f "$SYSTEMD_DIR/$f"; done
  rm -f "$RULE"
  $SYSTEMCTL daemon-reload
  ok "removed; the production site was not touched"
  warn "kept: the account $SVC_USER, $ROOT and the database $DB_NAME. To delete them for good:"
  warn "  sudo -u postgres dropdb $DB_NAME && sudo -u postgres dropuser $DB_NAME"
  warn "  sudo rm -rf $ROOT && sudo userdel $SVC_USER"
}

case "$MODE" in
  check)  preflight; say "checks passed; --apply sets the staging site up" ;;
  apply)  preflight; make_account; make_dirs; write_config; install_units; first_release
          say "done: $TAG answers on 127.0.0.1:$PORT"
          echo "    the first admin:   sudo -u $SVC_USER $ROOT/app/current/.venv/bin/aiscientist-admin create-admin <login>"
          echo "    from your machine: ssh -L $PORT:127.0.0.1:$PORT <you>@<this server>, then http://localhost:$PORT"
          echo "    on HPC3, once:     mkdir -m 2775 $HPC_ROOT  (group: the lab's)" ;;
  remove) remove ;;
esac
