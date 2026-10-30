#!/usr/bin/env bash
#
# Regenerate requirements.lock: the exact version and the hashes of every Python package the Eye
# Server's venv gets (the extras deploy/deploy_release.sh installs), resolved for the server's Python
# and platform. deploy_release.sh installs a release from it with `pip install --require-hashes`.
#
#   scripts/lock_requirements.sh                     # after a change to pyproject.toml: keeps every pin it can
#   scripts/lock_requirements.sh --upgrade           # the newest versions pyproject.toml allows
#   scripts/lock_requirements.sh -c <freeze.txt>     # prefer the versions a running venv has (pip freeze)
#
# Needs uv (`pip install uv`); it resolves for another Python and platform without either installed.
# LOCK_PYTHON is the server's Python version (default: the one the lock names, else 3.11),
# LOCK_PLATFORM its platform (default x86_64-manylinux_2_28). tests/test_requirements_lock.py fails
# when pyproject.toml asks for something the lock does not hold.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="$REPO/requirements.lock"
EXTRAS=(gateway auth anthropic langgraph analysis)      # deploy_release.sh's AISCIENTIST_EXTRAS
UV="${UV:-uv}"
command -v "$UV" >/dev/null 2>&1 || { echo "uv not found: pip install uv (or set UV=<path>)" >&2; exit 1; }

PY="${LOCK_PYTHON:-$( { sed -n 's/^# python: \([0-9.]*\).*/\1/p' "$LOCK" 2>/dev/null || true; } | head -n 1)}"
PY="${PY:-3.11}"
PLATFORM="${LOCK_PLATFORM:-x86_64-manylinux_2_28}"

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --upgrade) ARGS+=(--upgrade) ;;
    -c) ARGS+=(--constraint "$2"); shift ;;
    -h|--help) sed -n 2,16p "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# What builds the app itself (pip install --no-build-isolation -e <release>) is pinned too.
printf 'pip>=24\nsetuptools>=68\nwheel\n' > "$WORK/build.in"
[ ! -f "$LOCK" ] || cp "$LOCK" "$WORK/out.txt"       # the current pins are kept where they still fit

extra_args=()
for e in "${EXTRAS[@]}"; do extra_args+=(--extra "$e"); done
(cd "$REPO" && "$UV" pip compile pyproject.toml "$WORK/build.in" "${extra_args[@]}" \
   --python-version "$PY" --python-platform "$PLATFORM" --generate-hashes --no-header \
   --no-annotate --quiet ${ARGS[@]+"${ARGS[@]}"} -o "$WORK/out.txt")

{
  echo "# python: $PY ($PLATFORM)"
  echo "# The Eye Server's Python packages: pyproject.toml with the extras ${EXTRAS[*]},"
  echo "# plus what builds the app. Made by scripts/lock_requirements.sh; do not edit by hand."
  grep -v '^#' "$WORK/out.txt"
} > "$LOCK"
echo "requirements.lock: $(grep -c '^[a-z0-9]' "$LOCK") packages for Python $PY ($PLATFORM)"
