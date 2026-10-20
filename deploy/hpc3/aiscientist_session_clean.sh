#!/bin/bash
# AiScientist HPC3 session cleanup — remove what ONE ended session left on the cluster.
#
# The Temp sweeper (aiscientist_temp_gc.sh) removes process files by AGE: anything untouched for
# --ttl-days. This script removes them by OWNER, the moment a session ends, and takes only the
# targets it is handed by name:
#
#   --run <run_id>         <root>/Temp/<user>/<kind>/<run_id> under every kind (analysis, variant,
#                          phenotype, reports, scgpt, vlreview, corpus ...) — one run's folders
#   --session <id>         <root>/Temp/<user>/server-data/<id> — the copies of server-side datasets
#                          this session made (a new session copies them again)
#   --legacy-mirror <tag>  <root>/uploads/<user>/server-data/<tag> — where those copies were kept,
#                          shared between sessions, before they became per-session
#
# It never lists a directory to decide what to delete: a run or a session it is not told about is
# left alone, so another session's files cannot be reached from here. Nothing under
# <root>/uploads/<user>/ other than server-data/<tag> is ever considered — a dataset a user
# uploaded exists only there.
#
# Usage:
#   aiscientist_session_clean.sh --root <shared_root> --user <ucinetid>
#                                [--run <run_id>]... [--session <id>] [--legacy-mirror <tag>]...
#                                [--dry-run] [--quiet]
set -uo pipefail

ROOT=""
ONLY_USER=""
SESSION=""
RUNS=()
LEGACY=()
DRY=0
QUIET=0

die() { echo "aiscientist_session_clean: $*" >&2; exit 2; }

# One path component: letters, digits, _ and - only. No '/', no '.', so no traversal.
is_name() {
    case "$1" in
        ''|*[!a-zA-Z0-9_-]*) return 1 ;;
    esac
    return 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --root)          ROOT="${2-}"; shift 2 ;;
        --user)          ONLY_USER="${2-}"; shift 2 ;;
        --session)       SESSION="${2-}"; shift 2 ;;
        --run)           RUNS+=("${2-}"); shift 2 ;;
        --legacy-mirror) LEGACY+=("${2-}"); shift 2 ;;
        --dry-run)       DRY=1; shift ;;
        --quiet)         QUIET=1; shift ;;
        *)               die "unknown argument: $1" ;;
    esac
done

# --- guards: an absolute, traversal-free root, one named user, plain names for every target ----
case "$ROOT" in
    /*) ;;
    *)  die "--root must be an absolute path (got '${ROOT}')" ;;
esac
case "$ROOT" in
    *..*) die "--root must not contain '..'" ;;
esac
[ "$ROOT" = "/" ] && die "--root must not be /"
is_name "$ONLY_USER" || die "--user must be a bare account name (got '${ONLY_USER}')"
[ -z "$SESSION" ] || is_name "$SESSION" || die "--session must be a plain name (got '${SESSION}')"
for r in ${RUNS[@]+"${RUNS[@]}"}; do
    is_name "$r" || die "--run must be a plain name (got '${r}')"
done
for t in ${LEGACY[@]+"${LEGACY[@]}"}; do
    is_name "$t" || die "--legacy-mirror must be a plain name (got '${t}')"
done

TEMP="${ROOT%/}/Temp/${ONLY_USER}"
OLD_MIRROR="${ROOT%/}/uploads/${ONLY_USER}/server-data"

removed=0
missing=0
EMPTIED=()          # the kind dirs a run was removed from, to collapse if that left them empty

remove_dir() {
    local target="$1"
    # A real directory only. A link is never followed: whatever it points at is not this session's.
    if [ -L "$target" ] || [ ! -d "$target" ]; then
        missing=$((missing + 1))
        return
    fi
    if [ "$DRY" -eq 1 ]; then
        echo "would remove: ${target}"
    else
        rm -rf -- "$target" || { echo "aiscientist_session_clean: could not remove ${target}" >&2; return; }
        [ "$QUIET" -eq 1 ] || echo "removed: ${target}"
    fi
    removed=$((removed + 1))
}

for r in ${RUNS[@]+"${RUNS[@]}"}; do
    found=0
    for kind in "$TEMP"/*/; do
        [ -d "$kind" ] || continue                  # unexpanded glob / not a dir
        kind="${kind%/}"
        case "${kind##*/}" in
            scratch|server-data) continue ;;        # keyed by job line and by session, never by run
        esac
        if [ -d "$kind/$r" ] && [ ! -L "$kind/$r" ]; then
            found=1
            remove_dir "$kind/$r"
            EMPTIED+=("$kind")
        fi
    done
    [ "$found" -eq 1 ] || missing=$((missing + 1))
done

[ -z "$SESSION" ] || remove_dir "$TEMP/server-data/$SESSION"

for t in ${LEGACY[@]+"${LEGACY[@]}"}; do
    remove_dir "$OLD_MIRROR/$t"
done

# Collapse what the removals emptied, and only that: the kind dirs a run was just removed from,
# the per-session parent, and the old mirror's parent. rmdir removes an EMPTY directory or fails,
# so it cannot take a folder another session has put a run in since; a directory this script did
# not remove anything from (scratch/, paperqa/) is not looked at, empty or not.
if [ "$DRY" -eq 0 ]; then
    for kind in ${EMPTIED[@]+"${EMPTIED[@]}"}; do
        rmdir "$kind" 2>/dev/null
    done
    [ -z "$SESSION" ] || rmdir "$TEMP/server-data" 2>/dev/null
    [ "${#LEGACY[@]}" -eq 0 ] || rmdir "$OLD_MIRROR" 2>/dev/null
fi

[ "$QUIET" -eq 1 ] || echo "aiscientist_session_clean: removed=${removed} missing=${missing} user=${ONLY_USER}"
# Machine-readable last lines: the job (inside Slurm), so the gateway can tell this cleanup's result
# from a later one's, then the result.
[ -z "${SLURM_JOB_ID:-}" ] || echo "CLEAN_JOB ${SLURM_JOB_ID}"
echo "CLEAN_RESULT removed=${removed} missing=${missing}"
