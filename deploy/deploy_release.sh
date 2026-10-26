#!/usr/bin/env bash
#
# Versioned deploy of an AiScientist app release on the Eye Server.
#
#   deploy/deploy_release.sh <tag>        # e.g. v0.4.0: download, verify, install, switch, restart, health-check
#   deploy/deploy_release.sh --rollback   # switch back to the previous release and restart
#   deploy/deploy_release.sh --list       # show installed releases and which one is current
#
# Layout (under $AISCIENTIST_ROOT, default /data/BioAgent):
#   app/releases/<tag>/   one directory per release (code + VERSION)
#   app/current           symlink to the active release (what systemd runs)
#   app/previous          symlink to the release that was active before the last switch
#   env/<tag>/            one venv per release, so rollback needs no reinstall
#   shared/.env           secrets/config, linked into each release; never in Git
#
# If the health check fails after a switch, the script switches back to the previous release by
# itself. Releases beyond the newest $AISCIENTIST_KEEP (default 5) are pruned.
#
# Other env: AISCIENTIST_REPO (default RCHENLAB/AiScientist), AISCIENTIST_SERVICE (default bioagent),
# AISCIENTIST_PORT (8800), AISCIENTIST_EXTRAS (pip extras, default gateway), AISCIENTIST_PYTHON,
# AISCIENTIST_HEALTH_TIMEOUT (seconds, default 60), AISCIENTIST_PACKAGE_DIR (use a local
# aiscientist-<tag>.tar.gz + SHA256SUMS instead of downloading, for air-gapped hosts and tests).
set -euo pipefail

ROOT="${AISCIENTIST_ROOT:-/data/BioAgent}"
REPO="${AISCIENTIST_REPO:-RCHENLAB/AiScientist}"
SERVICE="${AISCIENTIST_SERVICE:-bioagent}"
PORT="${AISCIENTIST_PORT:-8800}"
EXTRAS="${AISCIENTIST_EXTRAS:-gateway}"
PYTHON="${AISCIENTIST_PYTHON:-python3}"
KEEP="${AISCIENTIST_KEEP:-5}"
HEALTH_TIMEOUT="${AISCIENTIST_HEALTH_TIMEOUT:-60}"
RESTART_CMD="${AISCIENTIST_RESTART_CMD:-sudo systemctl restart $SERVICE}"

APP_DIR="$ROOT/app"
RELEASES="$APP_DIR/releases"
CURRENT="$APP_DIR/current"
PREVIOUS="$APP_DIR/previous"
SHARED="$ROOT/shared"

die() { echo "ERROR: $*" >&2; exit 1; }

target_of() { [ -L "$1" ] && basename "$(readlink "$1")" || true; }

# Atomic symlink switch (ln -sfn would briefly leave a window on some filesystems; rename is atomic).
switch_to() {
  local link="$1" target="$2"
  ln -sfn "$target" "$link.tmp"
  mv -Tf "$link.tmp" "$link"
}

health_check() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT))
  until curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; do
    [ "$SECONDS" -lt "$deadline" ] || return 1
    sleep 2
  done
}

restart_and_check() {
  $RESTART_CMD
  health_check
}

rollback() {
  local prev cur
  prev="$(target_of "$PREVIOUS")"
  cur="$(target_of "$CURRENT")"
  [ -n "$prev" ] || die "no previous release recorded; nothing to roll back to"
  [ -d "$RELEASES/$prev" ] || die "previous release $prev is missing from $RELEASES"
  echo "==> rolling back $cur -> $prev"
  switch_to "$CURRENT" "releases/$prev"
  [ -z "$cur" ] || switch_to "$PREVIOUS" "releases/$cur"
  if restart_and_check; then
    echo "==> rollback complete; running $prev"
  else
    die "service did not become healthy after rollback to $prev; check: journalctl -u $SERVICE -n 50"
  fi
}

list_releases() {
  local cur
  cur="$(target_of "$CURRENT")"
  [ -d "$RELEASES" ] || { echo "no releases installed"; return 0; }
  for d in "$RELEASES"/*/; do
    [ -d "$d" ] || continue
    n="$(basename "$d")"
    echo "$n$([ "$n" = "$cur" ] && echo '  <- current')"
  done
}

fetch_package() {
  local tag="$1" dest="$2" name="aiscientist-$1.tar.gz"
  if [ -n "${AISCIENTIST_PACKAGE_DIR:-}" ]; then
    cp "$AISCIENTIST_PACKAGE_DIR/$name" "$AISCIENTIST_PACKAGE_DIR/SHA256SUMS" "$dest/"
  else
    local base="https://github.com/$REPO/releases/download/$tag"
    curl -fsSL -o "$dest/$name" "$base/$name"
    curl -fsSL -o "$dest/SHA256SUMS" "$base/SHA256SUMS"
  fi
  # SHA256SUMS lists only the package for this tag; refuse to install unless it verifies.
  (cd "$dest" && grep -F "  $name" SHA256SUMS | sha256sum -c -) || die "checksum verification failed for $name"
}

install_release() {
  local tag="$1"
  case "$tag" in v[0-9]*.[0-9]*.[0-9]*) ;; *) die "tag must look like v1.2.3 (got '$tag')";; esac
  local dest="$RELEASES/$tag" venv="$ROOT/env/$tag" old
  old="$(target_of "$CURRENT")"
  [ "$old" != "$tag" ] || { echo "$tag is already the current release"; exit 0; }

  mkdir -p "$RELEASES" "$ROOT/env" "$SHARED"
  if [ ! -d "$dest" ]; then
    local tmp
    tmp="$(mktemp -d "$APP_DIR/.fetch.XXXXXX")"
    trap "rm -rf '$tmp'" EXIT
    echo "==> downloading and verifying $tag"
    fetch_package "$tag" "$tmp"
    tar -C "$tmp" -xzf "$tmp/aiscientist-$tag.tar.gz"
    mv "$tmp/aiscientist-$tag" "$dest"
  fi

  [ -f "$SHARED/.env" ] && ln -sfn "$SHARED/.env" "$dest/.env" \
    || echo "WARNING: $SHARED/.env not found; the console needs its config there" >&2

  if [ ! -x "$venv/bin/python" ]; then
    echo "==> creating venv $venv"
    "$PYTHON" -m venv "$venv"
  fi
  echo "==> pip install $dest[$EXTRAS]"
  "$venv/bin/pip" install -q -U pip
  "$venv/bin/pip" install -q "$dest[$EXTRAS]"

  echo "==> initializing database schema"
  (cd "$dest" && set -a && { [ ! -f .env ] || . ./.env; } && set +a \
    && "$venv/bin/python" -c "from aiscientist.gateway.db import init_db; init_db()") \
    || echo "NOTE: database init skipped or failed (auth extra not installed?)" >&2

  ln -sfn "$venv" "$dest/.venv"
  echo "==> switching current -> $tag"
  [ -z "$old" ] || switch_to "$PREVIOUS" "releases/$old"
  switch_to "$CURRENT" "releases/$tag"

  if restart_and_check; then
    echo "==> $tag is healthy"
  else
    echo "ERROR: health check failed for $tag" >&2
    if [ -n "$old" ]; then
      rollback
    fi
    exit 1
  fi
  prune
}

prune() {
  local cur prev
  cur="$(target_of "$CURRENT")"; prev="$(target_of "$PREVIOUS")"
  # Newest first by modification time; always keep current and previous.
  ls -1t "$RELEASES" | tail -n +"$((KEEP + 1))" | while read -r n; do
    [ "$n" = "$cur" ] || [ "$n" = "$prev" ] || { echo "==> pruning $n"; rm -rf "${RELEASES:?}/$n" "${ROOT:?}/env/$n"; }
  done
}

case "${1:-}" in
  --rollback) rollback ;;
  --list)     list_releases ;;
  ""|-h|--help) sed -n 2,28p "$0" ;;
  *)          install_release "$1" ;;
esac
