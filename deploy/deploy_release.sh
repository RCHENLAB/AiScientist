#!/usr/bin/env bash
#
# Versioned deploy of an AiScientist app release on the Eye Server, run as the service account.
#
#   deploy/deploy_release.sh <tag>            # e.g. v0.4.0: download, verify, install, migrate, switch, restart, check
#   deploy/deploy_release.sh --prepare <tag>  # the same up to the database migration; nothing is switched or restarted
#   deploy/deploy_release.sh --rollback       # back to the previous release, and hold the one rolled back from
#   deploy/deploy_release.sh --auto           # what the deploy timer runs: the newest release, then the skills
#   deploy/deploy_release.sh --list           # installed releases, which one is current, which are held
#
# Layout (under $AISCIENTIST_ROOT, default /data/aiscientist):
#   app/releases/<tag>/   one release: the package's runtime files, VERSION, and its own venv in .venv/.
#                         Read-only once installed (AISCIENTIST_RELEASE_READONLY=0 keeps it writable)
#   app/current           symlink to the active release (what systemd runs)
#   app/previous          symlink to the release that was active before the last switch
#   app/held              tags --auto never installs: one rolled back from, or one whose check failed.
#                         Deploying a tag by name takes it off the list
#   config/.env           the deployment's settings and secrets, linked into each release. It must set
#                         AISCIENTIST_RESULTS_DIR, so run outputs never live inside a release
#   config/deploy.env     settings of this script and of deploy_skills.sh (KEY=value lines, below)
#   data/reference/genesets/*.gmt   the gene-set libraries (not in git), copied into each release
#   skills/               the skills releases and skills/current (deploy/deploy_skills.sh), linked into each release
#   logs/deploy.log       one line per install, switch, rollback and refusal
#
# A release's Python packages come from its requirements.lock (exact versions and hashes, for the
# server's Python) when the package has one, else from the pip extras. The database is migrated
# (aiscientist-admin migrate) before the switch; a migration only adds, so the release before it
# keeps working on the migrated database and a rollback needs no downgrade.
#
# A switch restarts aiscientist-keys.service first when it is installed (it runs the release's code).
# The health check needs /health to answer with this release's tag and with accounts on: a venv
# without the auth extra would serve the site with no login. If it fails after a switch, the script
# switches back to the release that was running and holds the failed one. A switch restarts the
# app, which interrupts runs in progress: a deploy waits while `aiscientist-admin active-runs` is
# not 0 (--auto tries again at its next start; a deploy by name stops unless AISCIENTIST_FORCE=1).
# Releases beyond the newest $AISCIENTIST_KEEP (default 5) are pruned; current and previous stay.
#
# --auto: the newest release of $AISCIENTIST_REPO that carries a package (or AISCIENTIST_APP_TAG
# when config/deploy.env pins one), when it is newer than the current one and not held; then
# deploy/deploy_skills.sh for the skills release AISCIENTIST_SKILLS_TAG names, else the one the
# current release pins in configs/skills_registry.json.
#
# Other env: AISCIENTIST_REPO (default RCHENLAB/AiScientist), AISCIENTIST_SERVICE (default
# aiscientist.service), AISCIENTIST_HOST (default: the --host the installed unit passes, else
# 127.0.0.1), AISCIENTIST_PORT (8800), AISCIENTIST_EXTRAS (pip extras, default
# gateway,auth,anthropic,langgraph,analysis; add corpus for the Eye Server copy of the RetiGene
# index, which installs CPU torch from AISCIENTIST_TORCH_INDEX first), AISCIENTIST_PYTHON (python3;
# its version must be the one requirements.lock was made for), AISCIENTIST_ENV_FILE (default
# $AISCIENTIST_ROOT/config/.env),
# AISCIENTIST_GENESETS_DIR_SRC (default $AISCIENTIST_ROOT/data/reference/genesets),
# AISCIENTIST_HEALTH_TIMEOUT (seconds, default 60), AISCIENTIST_RESTART_CMD,
# AISCIENTIST_PACKAGE_DIR (use local aiscientist-<tag>.tar.gz + SHA256SUMS instead of downloading,
# for air-gapped hosts and tests; --auto then picks the newest package there),
# AISCIENTIST_ALLOW_NO_ACCOUNTS=1 (accept a release with accounts off; never on a public host).
# Settings in the environment win over config/deploy.env. Needs GNU coreutils.
set -euo pipefail

ROOT="${AISCIENTIST_ROOT:-/data/aiscientist}"

# config/deploy.env: KEY=value lines, read as data (never sourced). The environment wins.
DEPLOY_ENV="${AISCIENTIST_DEPLOY_ENV:-$ROOT/config/deploy.env}"
if [ -f "$DEPLOY_ENV" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[[:space:]]*(AISCIENTIST_[A-Z0-9_]+)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
    val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
    [ -n "${!key+x}" ] || export "$key=$val"
  done < "$DEPLOY_ENV"
fi

REPO="${AISCIENTIST_REPO:-RCHENLAB/AiScientist}"
SERVICE="${AISCIENTIST_SERVICE:-aiscientist.service}"
PORT="${AISCIENTIST_PORT:-8800}"
EXTRAS="${AISCIENTIST_EXTRAS:-gateway,auth,anthropic,langgraph,analysis}"
PYTHON="${AISCIENTIST_PYTHON:-python3}"
KEEP="${AISCIENTIST_KEEP:-5}"
HEALTH_TIMEOUT="${AISCIENTIST_HEALTH_TIMEOUT:-60}"
RESTART_CMD="${AISCIENTIST_RESTART_CMD:-sudo -n systemctl restart $SERVICE}"
KEYS_SERVICE="${AISCIENTIST_KEYS_SERVICE:-aiscientist-keys.service}"
KEYS_RESTART_CMD="${AISCIENTIST_KEYS_RESTART_CMD:-sudo -n systemctl restart $KEYS_SERVICE}"
ENV_FILE="${AISCIENTIST_ENV_FILE:-$ROOT/config/.env}"
GENESETS_SRC="${AISCIENTIST_GENESETS_DIR_SRC:-$ROOT/data/reference/genesets}"
READONLY="${AISCIENTIST_RELEASE_READONLY:-1}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

APP_DIR="$ROOT/app"
RELEASES="$APP_DIR/releases"
CURRENT="$APP_DIR/current"
PREVIOUS="$APP_DIR/previous"
HELD="$APP_DIR/held"
LOG_FILE="$ROOT/logs/deploy.log"
TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+$'

die() { echo "ERROR: $*" >&2; log "error: $*"; exit 1; }
log() { mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) app $*" >> "$LOG_FILE" 2>/dev/null || true; }

# One deploy at a time (the timer and an operator), app and skills alike.
lock() {
  mkdir -p "$APP_DIR"
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$ROOT/.deploy.lock"
    flock -n 9 || { echo "another deploy is running (lock $ROOT/.deploy.lock); try again when it ends" >&2; exit 75; }
    export AISCIENTIST_DEPLOY_LOCKED=1     # deploy_skills.sh, run from here, already holds it
  fi
}

# The unit binds one routable address, not loopback (deploy/README.md), so probe what it binds.
unit_host() {
  command -v systemctl >/dev/null 2>&1 || return 0
  systemctl cat "$SERVICE" 2>/dev/null | sed -n 's/^ExecStart=.*--host[ =]\([^ ]*\).*/\1/p' | tail -n 1
}
HOST="${AISCIENTIST_HOST:-$(unit_host)}"
HOST="${HOST:-127.0.0.1}"
export AISCIENTIST_HOST="$HOST" AISCIENTIST_PORT="$PORT"     # deploy_skills.sh probes the same address

target_of() { [ -L "$1" ] && basename "$(readlink "$1")" || true; }

# Atomic symlink switch (ln -sfn would briefly leave a window on some filesystems; rename is atomic).
switch_to() {
  local link="$1" target="$2"
  ln -sfn "$target" "$link.tmp"
  mv -Tf "$link.tmp" "$link"
}

is_held() { [ -f "$HELD" ] && grep -qxF "$1" "$HELD"; }
hold() { is_held "$1" || echo "$1" >> "$HELD"; }
unhold() {
  [ -f "$HELD" ] || return 0
  grep -vxF "$1" "$HELD" > "$HELD.tmp" || true
  mv -f "$HELD.tmp" "$HELD"
}

# version_newer A B: A is a later version than B.
version_newer() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" = "$1" ]; }

# Healthy = /health answers with the expected release's tag and, unless allowed, accounts on.
health_check() {
  local want="$1" deadline=$((SECONDS + HEALTH_TIMEOUT)) body=""
  until body="$(curl -fsS --max-time 5 "http://$HOST:$PORT/health" 2>/dev/null)" \
        && printf '%s' "$body" | grep -q "\"version\":\"$want\""; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "ERROR: http://$HOST:$PORT/health did not report $want within ${HEALTH_TIMEOUT}s${body:+ (last answer: $body)}" >&2
      return 1
    fi
    sleep 2
  done
  if [ "${AISCIENTIST_ALLOW_NO_ACCOUNTS:-0}" != 1 ] && ! printf '%s' "$body" | grep -q '"accounts":true'; then
    echo "ERROR: $want answers with accounts off ($body): the site would have no login. Is the auth extra in AISCIENTIST_EXTRAS?" >&2
    return 1
  fi
}

# The key service (deploy/keys/, aiscientist-keys.service) runs the release's code too, and the app
# makes its API-key calls through it: when it is installed it restarts first (docs/key_service_design.md).
keys_installed() {
  [ -n "${AISCIENTIST_KEYS_RESTART_CMD:-}" ] && return 0
  command -v systemctl >/dev/null 2>&1 && systemctl cat "$KEYS_SERVICE" >/dev/null 2>&1
}

restart_and_check() {
  # 9>&-: what the restart starts must not inherit the deploy lock (lock()), or it holds it for as long
  # as it runs and every later deploy finds "another deploy is running".
  if keys_installed; then
    $KEYS_RESTART_CMD 9>&- || echo "WARNING: $KEYS_SERVICE did not restart: API-key calls fail until it runs" >&2
  fi
  $RESTART_CMD 9>&- || true    # the health check below decides; a unit whose start failed fails it too
  health_check "$1"
}

# How many runs the live app has in progress, or "?" when that cannot be read (no current release,
# a release without the command, no database).
active_runs() {
  local cur n
  cur="$(target_of "$CURRENT")"
  [ -n "$cur" ] && [ -x "$CURRENT/.venv/bin/python" ] || { echo "?"; return; }
  n="$(cd "$CURRENT" && "$CURRENT/.venv/bin/python" -m aiscientist.gateway.admin_cli active-runs 2>/dev/null | tail -n 1)" || n=""
  [[ "$n" =~ ^[0-9]+$ ]] && echo "$n" || echo "?"
}

# Stop unless no run is in progress. $1 = "auto": an unknown count also waits; a deploy by name
# goes ahead on an unknown count (the first install has no app to ask).
wait_for_idle() {
  local n
  n="$(active_runs)"
  if [ "$n" = "?" ]; then
    [ "$1" = auto ] && [ -n "$(target_of "$CURRENT")" ] && { echo "cannot tell whether runs are in progress; waiting"; exit 0; }
    return 0
  fi
  [ "$n" -eq 0 ] && return 0
  if [ "$1" = auto ]; then
    echo "$n run(s) in progress; a restart would interrupt them. Trying again at the next check."
    exit 0
  fi
  [ "${AISCIENTIST_FORCE:-0}" = 1 ] && { echo "WARNING: $n run(s) in progress are interrupted (AISCIENTIST_FORCE=1)" >&2; return 0; }
  die "$n run(s) in progress; a restart would interrupt them. Wait, or set AISCIENTIST_FORCE=1"
}

rollback() {
  lock
  local prev cur
  prev="$(target_of "$PREVIOUS")"
  cur="$(target_of "$CURRENT")"
  [ -n "$prev" ] || die "no previous release recorded; nothing to roll back to"
  [ -d "$RELEASES/$prev" ] || die "previous release $prev is missing from $RELEASES"
  echo "==> rolling back $cur -> $prev"
  switch_to "$CURRENT" "releases/$prev"
  [ -z "$cur" ] || switch_to "$PREVIOUS" "releases/$cur"
  # The timer must not install it again: it stays held until it is deployed by name.
  [ -z "$cur" ] || hold "$cur"
  if restart_and_check "$prev"; then
    log "rolled back $cur -> $prev; $cur held"
    echo "==> rollback complete; running $prev ($cur is held: --auto skips it)"
  else
    die "service did not become healthy after rollback to $prev; check: journalctl -u $SERVICE -n 50"
  fi
}

list_releases() {
  local cur prev n
  cur="$(target_of "$CURRENT")"; prev="$(target_of "$PREVIOUS")"
  [ -d "$RELEASES" ] || { echo "no releases installed"; return 0; }
  for d in "$RELEASES"/*/; do
    [ -d "$d" ] || continue
    n="$(basename "$d")"
    echo "$n$([ "$n" = "$cur" ] && echo '  <- current')$([ "$n" = "$prev" ] && echo '  <- previous')$(is_held "$n" && echo '  (held)')"
  done
}

asset_base() { echo "https://github.com/$REPO/releases/download/$1"; }

fetch_package() {
  local tag="$1" dest="$2" name="aiscientist-$1.tar.gz"
  if [ -n "${AISCIENTIST_PACKAGE_DIR:-}" ]; then
    cp "$AISCIENTIST_PACKAGE_DIR/$name" "$AISCIENTIST_PACKAGE_DIR/SHA256SUMS" "$dest/"
  else
    curl -fsSL -o "$dest/$name" "$(asset_base "$tag")/$name"
    curl -fsSL -o "$dest/SHA256SUMS" "$(asset_base "$tag")/SHA256SUMS"
  fi
  # SHA256SUMS lists only the package for this tag; refuse to install unless it verifies.
  (cd "$dest" && grep -F "  $name" SHA256SUMS | sha256sum -c -) || die "checksum verification failed for $name"
}

# The newest release with a package: the newest package in AISCIENTIST_PACKAGE_DIR, else the
# release GitHub calls latest (its redirect, which needs no API token and has no rate limit).
latest_tag() {
  if [ -n "${AISCIENTIST_PACKAGE_DIR:-}" ]; then
    (cd "$AISCIENTIST_PACKAGE_DIR" && ls aiscientist-v*.tar.gz 2>/dev/null) \
      | sed 's/^aiscientist-\(.*\)\.tar\.gz$/\1/' | grep -E "$TAG_RE" | sort -V | tail -n 1
    return 0
  fi
  local tag
  tag="$(curl -fsSI --max-time 20 "https://github.com/$REPO/releases/latest" 2>/dev/null \
         | tr -d '\r' | sed -n 's#^[Ll]ocation: .*/releases/tag/\(.*\)$#\1#p' | tail -n 1)" || tag=""
  [ -n "$tag" ] || return 0
  # A release made before the release workflow existed has no package: nothing to install.
  curl -fsSI --max-time 20 -o /dev/null "$(asset_base "$tag")/aiscientist-$tag.tar.gz" 2>/dev/null || {
    echo "the latest release $tag has no aiscientist-$tag.tar.gz; nothing to install" >&2; return 0; }
  echo "$tag"
}

pip_install() {
  local dest="$1" venv="$1/.venv"
  if [ ! -x "$venv/bin/python" ]; then
    echo "==> creating venv $venv"
    "$PYTHON" -m venv "$venv"
  fi
  export PIP_DISABLE_PIP_VERSION_CHECK=1
  local corpus=""
  [[ ",$EXTRAS," == *",corpus,"* ]] && corpus=1
  if [ -n "$corpus" ]; then
    # The corpus extra embeds on this server's CPU: CPU torch first, or pip pulls the CUDA build.
    "$venv/bin/pip" install -q torch --index-url "${AISCIENTIST_TORCH_INDEX:-https://download.pytorch.org/whl/cpu}"
  fi
  if [ -f "$dest/requirements.lock" ]; then
    # The lock is made for one Python version (its header names it); another would resolve differently.
    local want have
    want="$(sed -n 's/^# python: \([0-9.]*\).*/\1/p' "$dest/requirements.lock" | head -n 1)"
    have="$("$venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    [ -z "$want" ] || [ "$want" = "$have" ] \
      || die "$dest/requirements.lock is for Python $want, $PYTHON is $have: set AISCIENTIST_PYTHON"
    echo "==> pip install --require-hashes -r requirements.lock (Python $have)"
    "$venv/bin/pip" install -q --require-hashes -r "$dest/requirements.lock"
    "$venv/bin/pip" install -q --no-deps --no-build-isolation -e "$dest"
    # requirements.lock covers the default extras only; the corpus extra comes on top of it.
    [ -z "$corpus" ] || "$venv/bin/pip" install -q -e "$dest[corpus]"
  else
    echo "==> pip install -e $dest[$EXTRAS] (this package has no requirements.lock)"
    "$venv/bin/pip" install -q -U pip
    "$venv/bin/pip" install -q -e "$dest[$EXTRAS]"
  fi
}

migrate_db() {
  local dest="$1"
  echo "==> migrating the database"
  # The admin CLI reads the app's .env (here config/.env) with the app's own loader.
  if grep -q '"migrate"' "$dest/src/aiscientist/gateway/admin_cli.py" 2>/dev/null; then
    (cd "$dest" && "$dest/.venv/bin/python" -m aiscientist.gateway.admin_cli migrate)
  else    # a package from before migrations: create the tables, as its startup would
    (cd "$dest" && "$dest/.venv/bin/python" -c "from pathlib import Path
from aiscientist.core.config import load_project_env
load_project_env(Path('.'))
from aiscientist.gateway.db import init_db
init_db()")
  fi
}

# Download, verify and install <tag> into app/releases/<tag>, migrate the database. Switches nothing.
prepare_release() {
  local tag="$1" dest="$RELEASES/$1"
  [[ "$tag" =~ $TAG_RE ]] || die "tag must look like v1.2.3 (got '$tag')"
  [ -f "$ENV_FILE" ] || die "$ENV_FILE not found: the app's settings and secrets live there (deploy/README.md)"
  grep -qE '^(AISCIENTIST|BIOAGENT)_RESULTS_DIR=.' "$ENV_FILE" \
    || die "$ENV_FILE does not set AISCIENTIST_RESULTS_DIR: run outputs would be written inside the release and deleted when it is pruned"

  mkdir -p "$RELEASES"
  if [ -f "$dest/.installed" ]; then
    echo "==> $tag is installed already"
  else
    # An earlier attempt that stopped half-way leaves a release without .installed: start over.
    [ ! -e "$dest" ] || { chmod -R u+w "$dest"; rm -rf "$dest"; }
    local tmp
    tmp="$(mktemp -d "$APP_DIR/.fetch.XXXXXX")"
    trap "rm -rf '$tmp'" EXIT
    echo "==> downloading and verifying $tag"
    fetch_package "$tag" "$tmp"
    tar -C "$tmp" -xzf "$tmp/aiscientist-$tag.tar.gz"
    mv "$tmp/aiscientist-$tag" "$dest"

    ln -sfn "$ENV_FILE" "$dest/.env"
    mkdir -p "$dest/skills"
    ln -sfn "$ROOT/skills/releases" "$dest/skills/releases"
    ln -sfn "$ROOT/skills/current" "$dest/skills/current"
    # The gene sets are not in git; real copies, because the app tars its package for HPC3 jobs.
    if compgen -G "$GENESETS_SRC/*.gmt" >/dev/null; then
      mkdir -p "$dest/src/aiscientist/tools/genesets"
      cp -p "$GENESETS_SRC"/*.gmt "$dest/src/aiscientist/tools/genesets/"
    else
      echo "WARNING: no gene sets in $GENESETS_SRC: enrichment has no libraries (scripts/fetch_genesets.py)" >&2
    fi
    pip_install "$dest"
    "$dest/.venv/bin/python" -m compileall -q "$dest/src" >/dev/null 2>&1 || true
    touch "$dest/.installed"
    if [ "$READONLY" = 1 ]; then chmod -R a-w "$dest"; fi
    log "installed $tag"
  fi
  migrate_db "$dest" || die "database migration failed for $tag; nothing was switched"
}

install_release() {
  local tag="$1" old before_prev
  old="$(target_of "$CURRENT")"
  before_prev="$(target_of "$PREVIOUS")"
  [ "$old" != "$tag" ] || { echo "$tag is already the current release"; unhold "$tag"; exit 0; }
  # Installing and migrating restart nothing, so they go ahead while runs are in progress; the
  # switch waits for them (--auto finds the release ready at its next start).
  prepare_release "$tag"
  wait_for_idle "${2:-named}"

  echo "==> switching current -> $tag"
  [ -z "$old" ] || switch_to "$PREVIOUS" "releases/$old"
  switch_to "$CURRENT" "releases/$tag"
  unhold "$tag"

  if restart_and_check "$tag"; then
    log "switched ${old:-nothing} -> $tag"
    echo "==> $tag is healthy"
  else
    echo "ERROR: health check failed for $tag" >&2
    hold "$tag"
    if [ -n "$old" ]; then
      # Back to the release that was running, with the links as they were before this deploy, so a
      # later --rollback never lands on the release that just failed. It stays installed to inspect.
      echo "==> switching back to $old" >&2
      switch_to "$CURRENT" "releases/$old"
      if [ -n "$before_prev" ]; then switch_to "$PREVIOUS" "releases/$before_prev"; else rm -f "$PREVIOUS"; fi
      restart_and_check "$old" || die "$old did not come back either; check: journalctl -u $SERVICE -n 50"
      echo "==> running $old again; $tag stays in $RELEASES for inspection, held" >&2
    fi
    log "refused $tag (health check failed); running ${old:-nothing}; $tag held"
    exit 1
  fi
  prune
}

prune() {
  local cur prev
  cur="$(target_of "$CURRENT")"; prev="$(target_of "$PREVIOUS")"
  # Newest first by modification time; always keep current and previous.
  ls -1t "$RELEASES" | tail -n +"$((KEEP + 1))" | while read -r n; do
    [ "$n" = "$cur" ] || [ "$n" = "$prev" ] || {
      echo "==> pruning $n"; chmod -R u+w "${RELEASES:?}/$n"; rm -rf "${RELEASES:?}/$n"; log "pruned $n"; }
  done
}

# The skills release the deployment wants: config/deploy.env's AISCIENTIST_SKILLS_TAG, else the
# tag the current release pins.
wanted_skills_tag() {
  if [ -n "${AISCIENTIST_SKILLS_TAG:-}" ]; then echo "$AISCIENTIST_SKILLS_TAG"; return; fi
  { sed -n 's/.*"release_tag"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CURRENT/configs/skills_registry.json" 2>/dev/null \
      || true; } | head -n 1
}

auto() {
  lock
  local want cur
  want="${AISCIENTIST_APP_TAG:-$(latest_tag)}"
  cur="$(target_of "$CURRENT")"
  if [ -z "$want" ]; then
    echo "no release to install"
  elif ! [[ "$want" =~ $TAG_RE ]]; then
    die "the release to install is '$want', not a v1.2.3 tag"
  elif [ "$want" = "$cur" ]; then
    echo "app: $cur is current"
  elif is_held "$want"; then
    echo "app: $want is held (rolled back from, or its check failed); deploy it by name to try it again"
  elif [ -n "$cur" ] && version_newer "$cur" "$want" && [ -z "${AISCIENTIST_APP_TAG:-}" ]; then
    echo "app: $cur is newer than the latest release $want; not going back"
  else
    install_release "$want" auto
  fi

  local skills
  skills="$(wanted_skills_tag)"
  [ -n "$skills" ] || return 0
  if [ "$(target_of "$ROOT/skills/current")" != "${skills#v}" ]; then
    wait_for_idle auto
    "$HERE/deploy_skills.sh" --auto "$skills"
  fi
}

case "${1:-}" in
  --rollback) rollback ;;
  --list)     list_releases ;;
  --auto)     auto ;;
  --prepare)  [ -n "${2:-}" ] || die "--prepare needs a tag"; lock; prepare_release "$2"
              echo "==> $2 is ready in $RELEASES/$2; deploy/deploy_release.sh $2 switches to it" ;;
  ""|-h|--help) sed -n 2,52p "$0" ;;
  -*)         die "unknown option $1 (try --help)" ;;
  *)          lock; install_release "$1" ;;
esac
