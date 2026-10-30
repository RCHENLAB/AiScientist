#!/bin/bash
# AiScientist HPC3 session cleanup — remove what ONE ended session left on the cluster.
#
# The sweeper (aiscientist_temp_gc.sh) removes what sessions left by AGE: anything untouched for
# --ttl-days. This script removes it by OWNER, the moment a session ends, and takes only the
# targets it is handed by name.
#
# The session layout (docs/hpc3_storage_layout.md):
#
#   --session-dir <path>   <root>/sessions/<session-id> (repeatable): everything in it except logs/, runs/ and
#                          manifest.json (its app/, scripts/, inputs/, shell-home/, downloads/,
#                          jobs/). logs/ stays with this job's log in it, for the next connect to
#                          read; the sweep removes what is left once it is cold.
#   --keep-inputs          with --session-dir: leave inputs/
#   --run-dir <path>       <root>/sessions/<session-id>/runs/<run_id>: one run's folder
#
# Both must be real folders this user owns, below <root>/sessions.
#
# The retired layout, for runs that started before it changed:
#
#   --run <run_id>         <root>/Temp/<user>/<kind>/<run_id> under every kind (analysis, variant,
#                          phenotype, reports, scgpt, vlreview, corpus ...) — one run's folders
#   --session <id>         <root>/Temp/<user>/server-data/<id> — the copies of server-side datasets
#                          this session made (a new session copies them again)
#   --legacy-mirror <tag>  <root>/uploads/<user>/server-data/<tag> — where those copies were kept,
#                          shared between sessions, before they became per-session
#
#   --verify-dir <dir>     the stage-out manifests, one <dir>/<run_id>.tsv per run whose results
#                          were copied to the server: "<sha256>\t<size>\t<path under result/>"
#
# It never lists a directory to decide what to delete: a run or a session it is not told about is
# left alone, so another session's files cannot be reached from here. Nothing under
# <root>/uploads/<user>/ other than server-data/<tag> is ever considered — a dataset a user
# uploaded exists only there.
#
# A run's results are never removed before they are safely on the server (issue 6). When a run's
# folders hold a result/ with any file in it, the run is removed only if <verify-dir>/<run_id>.tsv
# lists every one of those files with the same size and SHA-256, which this script computes again
# here, on the compute node. A run without a manifest, or with one file that differs or is not
# listed, is kept whole and named on a KEPT line, and the job exits 3 so Slurm records it FAILED.
#
# Usage:
#   aiscientist_session_clean.sh --root <shared_root> --user <ucinetid>
#                                [--session-dir <path> [--keep-inputs]] [--run-dir <path>]...
#                                [--run <run_id>]... [--session <id>] [--legacy-mirror <tag>]...
#                                [--verify-dir <dir>] [--dry-run] [--quiet]
set -uo pipefail

ROOT=""
ONLY_USER=""
SESSION=""
RUNS=()
LEGACY=()
VERIFY=""
SESSION_DIRS=()
KEEP_INPUTS=0
RUN_DIRS=()
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
        --verify-dir)    VERIFY="${2-}"; shift 2 ;;
        --session-dir)   SESSION_DIRS+=("${2-}"); shift 2 ;;
        --keep-inputs)   KEEP_INPUTS=1; shift ;;
        --run-dir)       RUN_DIRS+=("${2-}"); shift 2 ;;
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
if [ -n "$VERIFY" ]; then
    case "$VERIFY" in
        /*) ;;
        *)  die "--verify-dir must be an absolute path (got '${VERIFY}')" ;;
    esac
    case "$VERIFY" in
        *..*) die "--verify-dir must not contain '..'" ;;
    esac
fi

TEMP="${ROOT%/}/Temp/${ONLY_USER}"
OLD_MIRROR="${ROOT%/}/uploads/${ONLY_USER}/server-data"
SESSIONS="${ROOT%/}/sessions"

# <root>/sessions/<id> (kind "session") or <root>/sessions/<id>/runs/<id> (kind "run"), by its
# shape alone: plain names, nothing else in the path.
session_shape() {
    local p="$1" rest sid run
    case "$p" in "$SESSIONS"/*) ;; *) return 1 ;; esac
    rest="${p#"$SESSIONS"/}"
    if [ "$2" = "session" ]; then
        is_name "$rest"
        return
    fi
    sid="${rest%%/*}"
    run="${rest#*/}"
    is_name "$sid" || return 1
    case "$run" in runs/*) run="${run#runs/}" ;; *) return 1 ;; esac
    is_name "$run"
}

# ...and, at removal time, a real folder this user owns, under a session folder this user owns.
session_owned() {
    local p="$1" rest sid
    rest="${p#"$SESSIONS"/}"
    sid="${rest%%/*}"
    [ -d "$SESSIONS/$sid" ] && [ ! -L "$SESSIONS/$sid" ] && [ -O "$SESSIONS/$sid" ] \
        && [ -d "$p" ] && [ ! -L "$p" ] && [ -O "$p" ]
}

for d in ${SESSION_DIRS[@]+"${SESSION_DIRS[@]}"}; do
    session_shape "$d" session || die "--session-dir must be <root>/sessions/<id> (got '${d}')"
done
for d in ${RUN_DIRS[@]+"${RUN_DIRS[@]}"}; do
    session_shape "$d" run || die "--run-dir must be <root>/sessions/<id>/runs/<run_id> (got '${d}')"
done

removed=0
missing=0
kept=0

sha256_of() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum -- "$1" | cut -d' ' -f1
    else
        shasum -a 256 -- "$1" | cut -d' ' -f1
    fi
}

# Are all of run $1's results (the result/ folders given after it) on the server, as its
# stage-out manifest says? Prints a KEPT line and returns 1 when the run has to stay. A run with no
# file under any result/ has nothing to lose.
results_match() {
    local r="$1" res tsv f rel want size sum
    shift
    for res in "$@"; do
        [ -d "$res" ] && [ ! -L "$res" ] || continue
        [ -n "$(find "$res" -type f -print 2>/dev/null | head -n 1)" ] || continue
        tsv="${VERIFY%/}/$r.tsv"
        if [ -z "$VERIFY" ] || [ ! -f "$tsv" ] || [ -L "$tsv" ]; then
            echo "KEPT run=$r reason=no-stage-out-manifest"
            return 1
        fi
        while IFS= read -r f; do
            rel="${f#"$res"/}"
            want="$(awk -F'\t' -v p="$rel" '$3 == p { print $1 " " $2; exit }' "$tsv")"
            if [ -z "$want" ]; then
                echo "KEPT run=$r reason=not-on-the-server file=$rel"
                return 1
            fi
            size="$(wc -c < "$f" | tr -d ' ')"
            if [ "$size" != "${want#* }" ]; then
                echo "KEPT run=$r reason=size-differs file=$rel"
                return 1
            fi
            sum="$(sha256_of "$f")"
            if [ "$sum" != "${want%% *}" ]; then
                echo "KEPT run=$r reason=checksum-differs file=$rel"
                return 1
            fi
        done < <(find "$res" -type f 2>/dev/null)
    done
    return 0
}

# The retired layout: a run's folders are Temp/<user>/<kind>/<run_id>.
results_are_home() {
    local r="$1" kind dirs=()
    for kind in "$TEMP"/*/; do
        [ -d "$kind" ] || continue
        kind="${kind%/}"
        case "${kind##*/}" in
            scratch|server-data) continue ;;
        esac
        dirs+=("$kind/$r/result")
    done
    results_match "$r" ${dirs[@]+"${dirs[@]}"}
}
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
    if ! results_are_home "$r"; then
        kept=$((kept + 1))
        continue
    fi
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

# --- the session layout ----------------------------------------------------------------------------
RUN_PARENTS=()
for d in ${RUN_DIRS[@]+"${RUN_DIRS[@]}"}; do
    r="${d##*/}"
    if ! session_owned "$d"; then
        missing=$((missing + 1))
        continue
    fi
    if ! results_match "$r" "$d"/*/result; then
        kept=$((kept + 1))
        continue
    fi
    remove_dir "$d"
    RUN_PARENTS+=("${d%/*}")
done

for SESSION_DIR in ${SESSION_DIRS[@]+"${SESSION_DIRS[@]}"}; do
    if session_owned "$SESSION_DIR"; then
        took=0
        for child in "$SESSION_DIR"/* "$SESSION_DIR"/.[!.]* "$SESSION_DIR"/..?*; do
            { [ -e "$child" ] || [ -L "$child" ]; } || continue
            case "${child##*/}" in
                logs|runs|manifest.json) continue ;;
                inputs) [ "$KEEP_INPUTS" -eq 1 ] && continue ;;
            esac
            if [ "$DRY" -eq 1 ]; then
                echo "would remove: ${child}"
            elif [ -d "$child" ] && [ ! -L "$child" ]; then
                rm -rf -- "$child" || echo "aiscientist_session_clean: could not remove ${child}" >&2
            else
                rm -f -- "$child" || echo "aiscientist_session_clean: could not remove ${child}" >&2
            fi
            took=1
        done
        [ "$took" -eq 0 ] || removed=$((removed + 1))
        RUN_PARENTS+=("$SESSION_DIR/runs")
    else
        missing=$((missing + 1))
    fi
done

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
    for parent in ${RUN_PARENTS[@]+"${RUN_PARENTS[@]}"}; do
        rmdir "$parent" 2>/dev/null
    done
fi

# The manifests did their job; a run that was kept leaves them for whoever looks at why.
if [ "$DRY" -eq 0 ] && [ -n "$VERIFY" ] && [ "$kept" -eq 0 ] && [ -d "$VERIFY" ] && [ ! -L "$VERIFY" ]; then
    case "$VERIFY" in
        "${ROOT%/}/bin/${ONLY_USER}/stageout/"*|"$SESSIONS"/*/logs/stageout/*) rm -rf -- "$VERIFY" ;;
    esac
fi

[ "$QUIET" -eq 1 ] || echo "aiscientist_session_clean: removed=${removed} missing=${missing} kept=${kept} user=${ONLY_USER}"
# Machine-readable last lines: the job (inside Slurm), so the gateway can tell this cleanup's result
# from a later one's, then the result.
[ -z "${SLURM_JOB_ID:-}" ] || echo "CLEAN_JOB ${SLURM_JOB_ID}"
echo "CLEAN_RESULT removed=${removed} missing=${missing} kept=${kept}"
[ "$kept" -eq 0 ] || exit 3
