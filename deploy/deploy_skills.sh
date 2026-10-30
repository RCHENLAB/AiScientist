#!/usr/bin/env bash
#
# Deploy a release of the aiscientist-skills registry on the Eye Server, run as the service account.
#
#   deploy/deploy_skills.sh <tag>       # e.g. v0.2.0: download, verify, install, switch skills/current, check
#   deploy/deploy_skills.sh --rollback  # back to skills/previous, and hold the release rolled back from
#   deploy/deploy_skills.sh --list      # installed skills releases, which one is current, which are held
#
# Layout (under $AISCIENTIST_ROOT, default /data/aiscientist):
#   skills/releases/<version>/              one release as published: catalog.json, the skills zip,
#                                           SHA256SUMS. Read-only once installed
#   skills/releases/<version>.install.json  tag, the commit the tag named at install time, when
#   skills/current, skills/previous         the release the app reads, and the one before it
#   skills/held                             releases deploy_release.sh --auto does not install again
#
# A release is installed only when every file SHA256SUMS lists matches it and catalog.json is the
# catalog of that tag: the same check the app makes before it reads a skill
# (aiscientist.agents.skills_registry.check_release). The app re-reads its skill library when
# skills/current changes, without a restart; the script then reads /health until the app reports
# the new release, and switches back if it does not. A run in progress picks the new skills up at
# its next step.
#
# Env: AISCIENTIST_SKILLS_REPO (default lijinbio/aiscientist-skills), AISCIENTIST_SKILLS_PACKAGE_DIR
# (a folder holding the release's files, instead of downloading), AISCIENTIST_HOST / AISCIENTIST_PORT
# (where /health answers; default the unit's --host and 8800), AISCIENTIST_HEALTH_TIMEOUT (default
# 30 s), AISCIENTIST_SKILLS_KEEP (default 5), AISCIENTIST_SKILLS_PYTHON (a Python that imports the app,
# default app/current/.venv/bin/python). Settings in config/deploy.env apply, as in deploy_release.sh.
set -euo pipefail

ROOT="${AISCIENTIST_ROOT:-/data/aiscientist}"
DEPLOY_ENV="${AISCIENTIST_DEPLOY_ENV:-$ROOT/config/deploy.env}"
if [ -f "$DEPLOY_ENV" ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[[:space:]]*(AISCIENTIST_[A-Z0-9_]+)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"; val="${BASH_REMATCH[2]}"
    val="${val%\"}"; val="${val#\"}"; val="${val%\'}"; val="${val#\'}"
    [ -n "${!key+x}" ] || export "$key=$val"
  done < "$DEPLOY_ENV"
fi

SKILLS_REPO="${AISCIENTIST_SKILLS_REPO:-lijinbio/aiscientist-skills}"
SERVICE="${AISCIENTIST_SERVICE:-aiscientist.service}"
PORT="${AISCIENTIST_PORT:-8800}"
HEALTH_TIMEOUT="${AISCIENTIST_HEALTH_TIMEOUT:-30}"
KEEP="${AISCIENTIST_SKILLS_KEEP:-5}"
CHECK_PY="${AISCIENTIST_SKILLS_PYTHON:-$ROOT/app/current/.venv/bin/python}"

DIR="$ROOT/skills"
RELEASES="$DIR/releases"
CURRENT="$DIR/current"
PREVIOUS="$DIR/previous"
HELD="$DIR/held"
LOG_FILE="$ROOT/logs/deploy.log"
TAG_RE='^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$'

die() { echo "ERROR: $*" >&2; log "error: $*"; exit 1; }
log() { mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null && echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) skills $*" >> "$LOG_FILE" 2>/dev/null || true; }

lock() {
  mkdir -p "$RELEASES"
  [ "${AISCIENTIST_DEPLOY_LOCKED:-0}" = 1 ] && return 0     # run by deploy_release.sh, which holds it
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$ROOT/.deploy.lock"
    flock -n 9 || { echo "another deploy is running (lock $ROOT/.deploy.lock); try again when it ends" >&2; exit 75; }
  fi
}

unit_host() {
  command -v systemctl >/dev/null 2>&1 || return 0
  systemctl cat "$SERVICE" 2>/dev/null | sed -n 's/^ExecStart=.*--host[ =]\([^ ]*\).*/\1/p' | tail -n 1
}
HOST="${AISCIENTIST_HOST:-$(unit_host)}"
HOST="${HOST:-127.0.0.1}"

target_of() { [ -L "$1" ] && basename "$(readlink "$1")" || true; }
switch_to() { ln -sfn "$2" "$1.tmp"; mv -Tf "$1.tmp" "$1"; }
is_held() { [ -f "$HELD" ] && grep -qxF "$1" "$HELD"; }
hold() { is_held "$1" || echo "$1" >> "$HELD"; }
unhold() { [ -f "$HELD" ] || return 0; grep -vxF "$1" "$HELD" > "$HELD.tmp" || true; mv -f "$HELD.tmp" "$HELD"; }

# The app's own check of a release (checksums, catalog version). Without the app (a first install
# before any app release), sha256sum -c has checked the files and the catalog's version is read here.
check_release() {
  local root="$1" tag="$2"
  if [ -x "$CHECK_PY" ]; then
    (cd /tmp && "$CHECK_PY" -c 'import sys
from aiscientist.agents.skills_registry import _main
sys.exit(_main(sys.argv[1:]))' check "$root" "$tag")
  else
    echo "WARNING: $CHECK_PY not found; checking the checksums and the catalog's version only" >&2
    (cd "$root" && sha256sum -c --quiet SHA256SUMS) && grep -q "\"registry_version\": *\"${tag#v}\"" "$root/catalog.json"
  fi
}

fetch_release() {
  local tag="$1" dest="$2" name
  local base="https://github.com/$SKILLS_REPO/releases/download/$tag"
  if [ -n "${AISCIENTIST_SKILLS_PACKAGE_DIR:-}" ]; then
    cp "$AISCIENTIST_SKILLS_PACKAGE_DIR/SHA256SUMS" "$dest/"
  else
    curl -fsSL -o "$dest/SHA256SUMS" "$base/SHA256SUMS" || die "$tag of $SKILLS_REPO has no SHA256SUMS"
  fi
  grep -q 'catalog\.json$' "$dest/SHA256SUMS" || die "SHA256SUMS of $tag does not list catalog.json"
  # Each file SHA256SUMS names, by a plain file name only.
  while read -r _ name; do
    name="${name#\*}"
    [[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "unexpected file name in SHA256SUMS: '$name'"
    if [ -n "${AISCIENTIST_SKILLS_PACKAGE_DIR:-}" ]; then
      cp "$AISCIENTIST_SKILLS_PACKAGE_DIR/$name" "$dest/"
    else
      curl -fsSL -o "$dest/$name" "$base/$name" || die "could not download $name of $tag"
    fi
  done < "$dest/SHA256SUMS"
  (cd "$dest" && sha256sum -c --quiet SHA256SUMS) || die "checksum verification failed for $tag"
}

# The commit the tag names now, for the System page (best effort: no token, so it may be rate-limited).
tag_commit() {
  [ -z "${AISCIENTIST_SKILLS_PACKAGE_DIR:-}" ] || { echo ""; return; }
  curl -fsS --max-time 15 "https://api.github.com/repos/$SKILLS_REPO/commits/$1" 2>/dev/null \
    | sed -n 's/^  "sha": "\([0-9a-f]\{40\}\)".*/\1/p' | head -n 1 || true
}

install_release() {
  local tag="$1" version="${1#v}"
  local dest="$RELEASES/$version"
  [[ "$tag" =~ $TAG_RE ]] || die "tag must look like v1.2.3 (got '$tag')"
  if [ -d "$dest" ]; then
    echo "==> skills $tag is installed already; checking it again"
    check_release "$dest" "$tag" || die "the installed skills release $tag fails its check: remove $dest and deploy it again"
    return 0
  fi
  local tmp
  tmp="$(mktemp -d "$DIR/.fetch.XXXXXX")"
  chmod 0755 "$tmp"                    # readable like the rest of skills/ (mktemp makes it 0700)
  trap "chmod -R u+w '$tmp' 2>/dev/null || true; rm -rf '$tmp'" EXIT
  echo "==> downloading and verifying skills $tag from $SKILLS_REPO"
  fetch_release "$tag" "$tmp"
  check_release "$tmp" "$tag" || die "skills $tag refused; nothing was installed"
  local commit
  commit="$(tag_commit "$tag")"
  printf '{"tag": "%s", "commit": %s, "repository": "%s", "installed_at": "%s"}\n' "$tag" \
    "$([ -n "$commit" ] && echo "\"$commit\"" || echo null)" "$SKILLS_REPO" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > "$RELEASES/$version.install.json"
  chmod -R a-w "$tmp"
  chmod u+w "$tmp"; mv "$tmp" "$dest"; chmod u-w "$dest"
  log "installed $tag${commit:+ ($commit)}"
}

# Wait until the running app reports the release. 0 = it does; 1 = it reports another one or
# refuses it; 2 = no app answers, or one that does not report its skills release.
app_loaded() {
  local want="$1" deadline=$((SECONDS + HEALTH_TIMEOUT)) body=""
  while :; do
    body="$(curl -fsS --max-time 5 "http://$HOST:$PORT/health" 2>/dev/null)" || body=""
    [ -n "$body" ] || return 2
    printf '%s' "$body" | grep -q '"skills":' || return 2
    printf '%s' "$body" | grep -q "\"skills\":\"$want\"" && return 0
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "ERROR: the app did not load skills $want within ${HEALTH_TIMEOUT}s (last answer: $body)" >&2
      return 1
    fi
    sleep 2
  done
}

switch_release() {
  local tag="$1" version="${1#v}" old before_prev rc=0
  old="$(target_of "$CURRENT")"; before_prev="$(target_of "$PREVIOUS")"
  [ "$old" != "$version" ] || { echo "skills $tag is current already"; unhold "$tag"; return 0; }
  echo "==> switching skills/current -> $version"
  [ -z "$old" ] || switch_to "$PREVIOUS" "releases/$old"
  switch_to "$CURRENT" "releases/$version"
  unhold "$tag"
  app_loaded "$tag" || rc=$?
  case "$rc" in
    0) echo "==> the app runs skills $tag"; log "switched ${old:+v$old}${old:-nothing} -> $tag" ;;
    2) echo "==> skills/current -> $version; no running app reports its skills release, so the next start reads it"
       log "switched ${old:+v$old}${old:-nothing} -> $tag (not confirmed by a running app)" ;;
    *) hold "$tag"
       if [ -n "$old" ]; then
         switch_to "$CURRENT" "releases/$old"
         if [ -n "$before_prev" ]; then switch_to "$PREVIOUS" "releases/$before_prev"; else rm -f "$PREVIOUS"; fi
         echo "==> back to skills v$old; $tag is held" >&2
       else
         rm -f "$CURRENT"
       fi
       log "refused $tag (the app did not load it); ${old:+back to v$old}; $tag held"
       exit 1 ;;
  esac
  prune
}

rollback() {
  local prev cur
  prev="$(target_of "$PREVIOUS")"; cur="$(target_of "$CURRENT")"
  [ -n "$prev" ] && [ -d "$RELEASES/$prev" ] || die "no previous skills release to roll back to"
  echo "==> rolling back skills v$cur -> v$prev"
  switch_to "$CURRENT" "releases/$prev"
  [ -z "$cur" ] || { switch_to "$PREVIOUS" "releases/$cur"; hold "v$cur"; }
  local rc=0
  app_loaded "v$prev" || rc=$?
  [ "$rc" != 1 ] || die "the app did not load skills v$prev after the rollback"
  log "rolled back v$cur -> v$prev; v$cur held"
  echo "==> running skills v$prev (v$cur is held: --auto skips it)"
}

list_releases() {
  local cur prev n
  cur="$(target_of "$CURRENT")"; prev="$(target_of "$PREVIOUS")"
  for d in "$RELEASES"/*/; do
    [ -d "$d" ] || continue
    n="$(basename "$d")"
    echo "v$n$([ "$n" = "$cur" ] && echo '  <- current')$([ "$n" = "$prev" ] && echo '  <- previous')$(is_held "v$n" && echo '  (held)')"
  done
}

prune() {
  local cur prev
  cur="$(target_of "$CURRENT")"; prev="$(target_of "$PREVIOUS")"
  ls -1t "$RELEASES" | grep -v '\.install\.json$' | tail -n +"$((KEEP + 1))" | while read -r n; do
    [ "$n" = "$cur" ] || [ "$n" = "$prev" ] || {
      echo "==> pruning skills v$n"; chmod -R u+w "${RELEASES:?}/$n"
      rm -rf "${RELEASES:?}/$n" "${RELEASES:?}/$n.install.json"; log "pruned v$n"; }
  done
}

case "${1:-}" in
  --rollback) lock; rollback ;;
  --list)     list_releases ;;
  --auto)     # from deploy_release.sh --auto: a held release is left alone
              [ -n "${2:-}" ] || die "--auto needs a tag"; lock
              if is_held "$2"; then echo "skills: $2 is held; deploy it by name to try it again"; exit 0; fi
              install_release "$2"; switch_release "$2" ;;
  ""|-h|--help) sed -n 2,27p "$0" ;;
  -*)         die "unknown option $1 (try --help)" ;;
  *)          lock; install_release "$1"; switch_release "$1" ;;
esac
