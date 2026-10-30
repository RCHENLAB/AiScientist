#!/usr/bin/env bash
#
# Move the Eye Server from the rsync layout to the release layout, once (RCHENLAB issue 29). Run ON
# the server as root, from the release package it switches to:
#
#   cd /tmp && curl -fsSLO https://github.com/RCHENLAB/AiScientist/releases/download/<tag>/aiscientist-<tag>.tar.gz
#   curl -fsSLO https://github.com/RCHENLAB/AiScientist/releases/download/<tag>/SHA256SUMS
#   sha256sum -c SHA256SUMS && tar xzf aiscientist-<tag>.tar.gz
#   sudo bash aiscientist-<tag>/deploy/migrate_release_layout.sh --tag <tag>             # checks; changes nothing
#   sudo bash aiscientist-<tag>/deploy/migrate_release_layout.sh --tag <tag> --prepare   # site stays up
#   sudo bash aiscientist-<tag>/deploy/migrate_release_layout.sh --tag <tag> --apply     # about a minute down
#   sudo bash aiscientist-<tag>/deploy/migrate_release_layout.sh --rollback              # back to the rsync layout
#
# --prepare (no restart): config/.env (a copy of app/.env), config/deploy.env, the gene sets in
#   data/reference/genesets/, skills/current -> the installed release, and the release itself in
#   app/releases/<tag>/ with its venv, by deploy_release.sh --prepare as the service account. That
#   also migrates the database: the first time, it records the existing schema as revision 0001.
# --apply: waits for no run in progress, copies app/.env again, stops the unit, moves the rsync tree
#   (everything in app/ but releases/) and the old venv env/ into backups/release-layout-<stamp>/,
#   links app/current, installs the unit drop-in and the service account's restart rule, points the
#   key service (aiscientist-keys.service, when installed) at app/current's venv, restarts it, starts the
#   unit and checks /health. If the check fails it rolls back on its own.
# --rollback: the reverse, from the newest backups/release-layout-<stamp>/. config/, the gene-set
#   copy, skills/current and the database's revision table stay; the rsync layout ignores them.
#
# --package-dir <dir> installs aiscientist-<tag>.tar.gz + SHA256SUMS from <dir> instead of GitHub.
# --timer also enables aiscientist-deploy.timer: from then on new releases deploy themselves.
#
# Never run scripts/sync_deploy.sh between --prepare and --apply: with --delete it removes
# app/releases/. Rehearse it on a scratch root first: deploy/rehearsal/release_layout.sh.
# For that, every system path is a setting: AISCIENTIST_ROOT, SYSTEMD_DIR, SUDOERS_DIR, SYSTEMCTL,
# SVC_USER, AS_SVC ("" = run as yourself), REQUIRE_ROOT=0, BIND_HOST, PORT.
set -euo pipefail

ROOT="${AISCIENTIST_ROOT:-/data/aiscientist}"
SVC_USER="${SVC_USER:-aiscientist}"
UNIT="${UNIT:-aiscientist.service}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
SUDOERS_DIR="${SUDOERS_DIR:-/etc/sudoers.d}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
REQUIRE_ROOT="${REQUIRE_ROOT:-1}"
AS_SVC="${AS_SVC-sudo -u $SVC_USER}"
PORT="${PORT:-8800}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-120}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="$(cd "$HERE/.." && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
APP="$ROOT/app"
DROPIN="$SYSTEMD_DIR/$UNIT.d/release-layout.conf"
RULE="$SUDOERS_DIR/aiscientist-restart"
KEYS_UNIT="${KEYS_UNIT:-aiscientist-keys.service}"      # deploy/keys/install_key_service.sh, when installed
KEYS_FILE="$SYSTEMD_DIR/$KEYS_UNIT"

MODE=check TAG="" PKG_DIR="" TIMER=0
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift ;;
    --prepare) MODE=prepare ;;
    --apply) MODE=apply ;;
    --rollback) MODE=rollback ;;
    --package-dir) PKG_DIR="$(cd "$2" && pwd)"; shift ;;
    --timer) TIMER=1 ;;
    -h|--help) sed -n 2,33p "$0"; exit 0 ;;
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
FAILED=0
WHO="${AS_SVC:+$SVC_USER}"; WHO="${WHO:-$(id -un)}"

[ "$REQUIRE_ROOT" != 1 ] || [ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0 ...)"
[ "$MODE" = rollback ] || [[ "$TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "--tag v<major>.<minor>.<patch> is required"

unit_file() { echo "$SYSTEMD_DIR/$UNIT"; }
unit_host() { sed -n 's/^ExecStart=.*--host[ =]\([^ ]*\).*/\1/p' "$(unit_file)" 2>/dev/null | tail -n 1; }
BIND_HOST="${BIND_HOST:-$(unit_host)}"
BIND_HOST="${BIND_HOST:-127.0.0.1}"
env_value() { { grep -E "^(AISCIENTIST|BIOAGENT)_$1=" "$2" 2>/dev/null || true; } | tail -n 1 | cut -d= -f2- | tr -d '"'"'"; }
health() { curl -fsS --max-time 5 "http://$BIND_HOST:$PORT/health" 2>/dev/null || true; }

# Runs the gateway has in progress, read from the database with the rsync layout's own venv: rows
# still 'running' belong to the live process (its startup closed the ones a dead one left behind).
active_runs() {
  as_svc env AISCIENTIST_APP="$APP" "$ROOT/env/bin/python" - <<'PY' 2>/dev/null || echo "?"
import os
from pathlib import Path
from aiscientist.core.config import load_project_env
load_project_env(Path(os.environ["AISCIENTIST_APP"]))
from sqlalchemy import create_engine, text
from aiscientist.gateway.db import database_url
with create_engine(database_url()).connect() as c:
    print(c.execute(text("SELECT count(*) FROM runs WHERE status = 'running'")).scalar())
PY
}

preflight() {
  say "preflight ($ROOT, unit $UNIT, health on $BIND_HOST:$PORT)"
  if [ -L "$APP/current" ]; then
    bad "$APP/current exists: the release layout is in place already"; return
  fi
  [ -f "$APP/pyproject.toml" ] && ok "rsync layout: $APP holds the code ($(cat "$APP/.deployed_sha" 2>/dev/null | cut -c1-12))" \
    || bad "$APP/pyproject.toml missing: not the rsync layout this script expects"
  [ -x "$ROOT/env/bin/python" ] && ok "its venv: $ROOT/env" || bad "$ROOT/env/bin/python missing"
  local envf="$APP/.env" results state
  if [ -f "$envf" ]; then
    results="$(env_value RESULTS_DIR "$envf")"; state="$(env_value STATE_DIR "$envf")"
    case "$results" in
      "") bad ".env does not set AISCIENTIST_RESULTS_DIR (deploy_release.sh refuses)" ;;
      "$APP"/*) bad "AISCIENTIST_RESULTS_DIR=$results is inside app/, which moves" ;;
      *) ok "results outside the code: $results" ;;
    esac
    case "$state" in
      ""|"$APP"/*) bad "AISCIENTIST_STATE_DIR='$state' must be set and outside app/" ;;
      *) ok "state outside the code: $state" ;;
    esac
    if grep -qE '^(AISCIENTIST|BIOAGENT)_[A-Z_]+=.*'"$APP"'/' "$envf"; then
      bad ".env names paths inside $APP/: $(grep -oE '^(AISCIENTIST|BIOAGENT)_[A-Z_]+=.*'"$APP"'/[^ ]*' "$envf" | cut -d= -f1 | tr '\n' ' ')"
    fi
  else
    bad "$envf missing"
  fi
  if [ -e "$ROOT/config/.env" ]; then
    cmp -s "$envf" "$ROOT/config/.env" && ok "config/.env = app/.env" || warn "config/.env differs from app/.env: --prepare and --apply copy app/.env over it"
  fi
  [ -f "$(unit_file)" ] && ok "unit $(unit_file), --host $BIND_HOST" || bad "$(unit_file) missing"
  if [ -f "$KEYS_FILE" ]; then
    if grep -qF "$ROOT/env/bin/python" "$KEYS_FILE"; then
      ok "$KEYS_UNIT runs $ROOT/env/bin/python: --apply points it at app/current/.venv and restarts it first"
    else
      ok "$KEYS_UNIT installed ($(sed -n 's/^ExecStart=\([^ ]*\).*/\1/p' "$KEYS_FILE" | tail -n 1)); --apply restarts it first"
    fi
  fi
  [ ! -e "$DROPIN" ] && ok "no release-layout drop-in yet" || warn "$DROPIN exists already"
  local n
  n="$(ls "$APP"/src/aiscientist/tools/genesets/*.gmt 2>/dev/null | wc -l | tr -d ' ')"
  [ "$n" -gt 0 ] && ok "$n gene-set libraries to copy to data/reference/genesets/" \
    || warn "no gene sets in app/: enrichment will have none (scripts/fetch_genesets.py)"
  local skills_tag skills_root
  skills_tag="$(sed -n 's/.*"release_tag"[^"]*"v\([^"]*\)".*/\1/p' "$APP/configs/skills_registry.json" 2>/dev/null)"
  skills_root="$(env_value SKILLS_REGISTRY_RELEASES_ROOT "$envf")"; skills_root="${skills_root:-$ROOT/skills/releases}"
  if [ -L "$ROOT/skills/current" ]; then
    ok "skills/current -> $(readlink "$ROOT/skills/current")"
  elif [ -d "$skills_root/$skills_tag" ] && [ "$skills_root" = "$ROOT/skills/releases" ]; then
    ok "skills v$skills_tag installed; --prepare links skills/current to it"
  else
    bad "the pinned skills release v$skills_tag is not in $ROOT/skills/releases/"
  fi
  local py pybin="${AISCIENTIST_PYTHON:-python3}"
  py="$(as_svc "$pybin" -c 'import sys, ensurepip; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)"
  [ -n "$py" ] && ok "$pybin is Python $py, with venv support, as $WHO" \
    || bad "$pybin -m venv does not work as $WHO (python3-venv? AISCIENTIST_PYTHON names another Python)"
  if [ -f "$PKG/requirements.lock" ]; then
    local want; want="$(sed -n 's/^# python: \([0-9.]*\).*/\1/p' "$PKG/requirements.lock" | head -n 1)"
    [ "$want" = "$py" ] && ok "requirements.lock is for Python $want" || bad "requirements.lock is for Python $want, the server has $py"
  fi
  local code
  for url in https://github.com https://pypi.org/simple/pip/ https://files.pythonhosted.org/; do
    code="$(as_svc curl -sS --max-time 15 -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)"
    [ -n "$code" ] && [ "$code" != 000 ] && ok "reaches $url (HTTP $code)" || bad "cannot reach $url as $WHO"
  done
  as_svc test -w "$ROOT" -a -w "$APP" && ok "$ROOT and app/ writable by $WHO" || bad "$WHO cannot write $ROOT or $APP"
  # Anything else that names the old venv or code paths breaks when --apply moves them.
  local refs
  refs="$(grep -rlsF -e "$ROOT/env/" -e "$APP/src" "$SYSTEMD_DIR" /etc/cron.d /etc/crontab 2>/dev/null | grep -v -e "/$UNIT\$" -e "/$KEYS_UNIT\$" || true)"
  if [ "$REQUIRE_ROOT" = 1 ]; then
    crontab -l -u "$SVC_USER" 2>/dev/null | grep -qF -e "$ROOT/env/" -e "$APP/" && refs="$refs crontab($SVC_USER)"
  fi
  [ -z "$refs" ] && ok "nothing else names $ROOT/env or $APP/src" || warn "these name paths --apply moves: $refs"
  local free; free="$(df -Pk "$ROOT" | awk 'NR==2 {print int($4/1048576)}')"
  [ "$free" -ge 15 ] && ok "${free} GB free on $ROOT" || bad "only ${free} GB free on $ROOT (a release with its venv needs a few GB)"
  command -v flock >/dev/null && ok "flock" || warn "no flock: the deploy timer and an operator are not kept from overlapping"
  n="$(active_runs)"
  case "$n" in
    0) ok "no run in progress" ;;
    "?") warn "could not count the runs in progress" ;;
    *) warn "$n run(s) in progress: --apply waits for them (FORCE=1 interrupts them)" ;;
  esac
  [ -n "$(health)" ] && ok "the site answers /health: $(health)" || warn "nothing answers http://$BIND_HOST:$PORT/health"
}

prepare() {
  say "config/.env, config/deploy.env"
  [ -d "$ROOT/config" ] || { as_svc mkdir -p "$ROOT/config"; as_svc chmod 0700 "$ROOT/config"; }
  as_svc cp -p "$APP/.env" "$ROOT/config/.env"
  as_svc chmod 0640 "$ROOT/config/.env"
  if [ ! -e "$ROOT/config/deploy.env" ]; then
    as_svc tee "$ROOT/config/deploy.env" >/dev/null <<'EOF'
# Settings of deploy/deploy_release.sh and deploy/deploy_skills.sh (KEY=value; the deploy timer reads them).
# Where releases come from:
AISCIENTIST_REPO=RCHENLAB/AiScientist
AISCIENTIST_SKILLS_REPO=lijinbio/aiscientist-skills
# Hold the app at one release instead of following the newest one:
#AISCIENTIST_APP_TAG=v0.4.0
# The skills release; unset = the one the running app release pins (configs/skills_registry.json):
#AISCIENTIST_SKILLS_TAG=v0.1.0
EOF
  fi
  ok "config/: $(ls "$ROOT/config" | tr '\n' ' ')"

  say "gene sets -> data/reference/genesets/"
  as_svc mkdir -p "$ROOT/data/reference/genesets"
  if compgen -G "$APP/src/aiscientist/tools/genesets/*.gmt" >/dev/null; then
    as_svc cp -p "$APP"/src/aiscientist/tools/genesets/*.gmt "$ROOT/data/reference/genesets/"
  fi
  ok "$(ls "$ROOT/data/reference/genesets" | wc -l | tr -d ' ') file(s)"

  say "skills/current"
  if [ ! -L "$ROOT/skills/current" ]; then
    local v; v="$(sed -n 's/.*"release_tag"[^"]*"v\([^"]*\)".*/\1/p' "$APP/configs/skills_registry.json")"
    [ -d "$ROOT/skills/releases/$v" ] || die "skills release $v is not installed in $ROOT/skills/releases"
    as_svc ln -s "releases/$v" "$ROOT/skills/current"
  fi
  ok "skills/current -> $(readlink "$ROOT/skills/current")"

  say "release $TAG in app/releases/ (deploy_release.sh --prepare, as $WHO)"
  as_svc env AISCIENTIST_ROOT="$ROOT" AISCIENTIST_HOST="$BIND_HOST" AISCIENTIST_PORT="$PORT" \
    ${PKG_DIR:+AISCIENTIST_PACKAGE_DIR="$PKG_DIR"} ${AISCIENTIST_PYTHON:+AISCIENTIST_PYTHON="$AISCIENTIST_PYTHON"} \
    bash "$HERE/deploy_release.sh" --prepare "$TAG"
  ok "$APP/releases/$TAG is ready; the site still runs the rsync layout"
}

dropin_text() {
  sed "s#/data/aiscientist/#$ROOT/#g; s/128\.200\.71\.197/$BIND_HOST/g; s/:8800/:$PORT/g; s/--port 8800/--port $PORT/g" \
    "$APP/releases/$TAG/deploy/systemd/aiscientist.service.d/release-layout.conf"
}

apply() {
  local set="$ROOT/backups/release-layout-$STAMP" n
  [ -f "$APP/releases/$TAG/.installed" ] || prepare
  n="$(active_runs)"
  if [ "$n" != 0 ] && [ "$n" != "?" ] && [ "${FORCE:-0}" != 1 ]; then
    die "$n run(s) in progress; the switch restarts the app. Wait for them, or FORCE=1"
  fi
  say "backup set $set"
  as_svc mkdir -p "$set/app-rsync" && as_svc chmod 0700 "$set"
  cp -p "$(unit_file)" "$set/" 2>/dev/null || true
  [ ! -f "$KEYS_FILE" ] || cp -p "$KEYS_FILE" "$set/"
  as_svc cp -p "$APP/.env" "$set/app.env"
  printf 'TAG=%s\nSTAMP=%s\nSTATE=started\n' "$TAG" "$STAMP" | as_svc tee "$set/record" >/dev/null
  as_svc cp -p "$APP/.env" "$ROOT/config/.env"          # an edit since --prepare comes along

  say "stop $UNIT"
  $SYSTEMCTL stop "$UNIT"
  say "move the rsync tree and its venv into the backup set"
  (cd "$APP" && for e in .[!.]* ..?* *; do
     [ -e "$e" ] || [ -L "$e" ] || continue
     case "$e" in releases|.fetch.*) continue ;; esac
     as_svc mv "$APP/$e" "$set/app-rsync/"
   done)
  as_svc mv "$ROOT/env" "$set/env"
  as_svc ln -s "releases/$TAG" "$APP/current"
  echo "STATE=moved" | as_svc tee -a "$set/record" >/dev/null

  say "unit drop-in $DROPIN and the restart rule $RULE"
  mkdir -p "$(dirname "$DROPIN")"
  dropin_text > "$DROPIN.new" && mv -f "$DROPIN.new" "$DROPIN"
  local systemctl_bin; systemctl_bin="$(command -v systemctl || echo /usr/bin/systemctl)"
  printf '# Installed by deploy/migrate_release_layout.sh on %s: deploy/deploy_release.sh restarts the app\n# and the key service.\n%s ALL=(root) NOPASSWD: %s restart %s, %s restart %s\n' \
    "$STAMP" "$SVC_USER" "$systemctl_bin" "$UNIT" "$systemctl_bin" "$KEYS_UNIT" > "$RULE.new"
  if command -v visudo >/dev/null && [ "$REQUIRE_ROOT" = 1 ]; then visudo -cf "$RULE.new" >/dev/null || die "the sudoers rule does not parse"; fi
  chmod 0440 "$RULE.new" && mv -f "$RULE.new" "$RULE"
  if [ -f "$KEYS_FILE" ]; then         # the key service runs the release's code, from app/current
    sed -i.tmp "s#$ROOT/env/bin/python#$ROOT/app/current/.venv/bin/python#g" "$KEYS_FILE" && rm -f "$KEYS_FILE.tmp"
  fi
  for f in aiscientist-deploy.service aiscientist-deploy.timer; do
    cp "$APP/releases/$TAG/deploy/systemd/$f" "$SYSTEMD_DIR/$f"
  done
  $SYSTEMCTL daemon-reload
  echo "STATE=installed" | as_svc tee -a "$set/record" >/dev/null

  say "start $UNIT from app/current -> releases/$TAG"
  [ ! -f "$KEYS_FILE" ] || $SYSTEMCTL restart "$KEYS_UNIT" || echo "WARNING: $KEYS_UNIT did not restart" >&2
  $SYSTEMCTL start "$UNIT" || true
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) body=""
  until body="$(health)"; printf '%s' "$body" | grep -q "\"version\":\"$TAG\"" \
        && printf '%s' "$body" | grep -q '"accounts":true'; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "the release did not answer /health with $TAG and accounts on (last: ${body:-nothing}); rolling back" >&2
      rollback "$set"
      exit 1
    fi
    sleep 2
  done
  echo "STATE=applied" | as_svc tee -a "$set/record" >/dev/null
  ok "/health: $body"
  if [ "$TIMER" = 1 ]; then
    $SYSTEMCTL enable --now aiscientist-deploy.timer && ok "aiscientist-deploy.timer enabled: new releases deploy themselves"
  else
    warn "aiscientist-deploy.timer is installed, not enabled: $SYSTEMCTL enable --now aiscientist-deploy.timer"
  fi
  say "done: $TAG runs from $APP/current; the rsync tree is in $set (rollback: $0 --rollback)"
}

rollback() {
  local set="${1:-$(ls -d "$ROOT"/backups/release-layout-* 2>/dev/null | sort | tail -n 1)}"
  [ -n "$set" ] && [ -f "$set/record" ] || die "no backups/release-layout-*/record to roll back from"
  [ -d "$set/app-rsync" ] && [ -n "$(ls -A "$set/app-rsync")" ] || die "$set/app-rsync is empty: was the switch made?"
  say "rollback from $set"
  $SYSTEMCTL disable --now aiscientist-deploy.timer >/dev/null 2>&1 || true
  rm -f "$SYSTEMD_DIR/aiscientist-deploy.service" "$SYSTEMD_DIR/aiscientist-deploy.timer" "$DROPIN" "$RULE"
  rmdir "$(dirname "$DROPIN")" 2>/dev/null || true
  $SYSTEMCTL daemon-reload
  $SYSTEMCTL stop "$UNIT" || true
  as_svc mkdir -p "$set/release-layout"
  for e in releases current previous held; do
    [ -e "$APP/$e" ] || [ -L "$APP/$e" ] || continue
    as_svc mv "$APP/$e" "$set/release-layout/"
  done
  (cd "$set/app-rsync" && for e in .[!.]* ..?* *; do
     [ -e "$e" ] || [ -L "$e" ] || continue
     as_svc mv "$set/app-rsync/$e" "$APP/"
   done)
  [ ! -d "$set/env" ] || as_svc mv "$set/env" "$ROOT/env"
  if [ -f "$set/$KEYS_UNIT" ]; then                      # as it was before --apply
    cp -p "$set/$KEYS_UNIT" "$KEYS_FILE"
  elif [ -f "$KEYS_FILE" ]; then                         # installed after the move: back to the old venv
    sed -i.tmp "s#$ROOT/app/current/.venv/bin/python#$ROOT/env/bin/python#g" "$KEYS_FILE" && rm -f "$KEYS_FILE.tmp"
  fi
  if [ -f "$KEYS_FILE" ]; then
    $SYSTEMCTL daemon-reload
    $SYSTEMCTL restart "$KEYS_UNIT" || echo "WARNING: $KEYS_UNIT did not restart" >&2
  fi
  $SYSTEMCTL start "$UNIT"
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  until [ -n "$(health)" ]; do
    [ "$SECONDS" -lt "$deadline" ] || die "the rsync layout did not answer /health either: journalctl -u $UNIT -n 50"
    sleep 2
  done
  echo "STATE=rolled-back" | as_svc tee -a "$set/record" >/dev/null
  ok "/health: $(health)"
  say "back on the rsync layout; the release stays in $set/release-layout/"
}

case "$MODE" in
  check)    preflight; echo; [ "$FAILED" -eq 0 ] && echo "preflight passed; nothing was changed" \
              || { echo "preflight: $FAILED problem(s); nothing was changed"; exit 1; } ;;
  prepare)  preflight; [ "$FAILED" -eq 0 ] || die "fix the preflight first"; prepare ;;
  apply)    preflight; [ "$FAILED" -eq 0 ] || die "fix the preflight first"; apply ;;
  rollback) rollback ;;
esac
