#!/bin/bash
# AiScientist HPC3 sweeper — remove what sessions left once it has gone cold.
#
# Everything AiScientist generates on HPC3 lands under ONE shared project root
# (docs/hpc3_storage_layout.md):
#
#   <root>/sessions/<session-id>/          one session's folder          <- SWEPT
#   <root>/Temp/<ucinetid>/<kind>/<entry>  the retired per-user layout   <- SWEPT until it is empty
#   <root>/{envs,containers,models}/       environments, images, weights (never touched)
#   <root>/uploads/<ucinetid>/...          raw data of the retired layout (never removed; an empty
#                                          folder goes with --retire-legacy)
#
# Session folders: only those this account owns (`-O`) and that are not named by --keep-session
# (the account's live sessions). A cold session folder loses its app/, scripts/, inputs/,
# shell-home/, downloads/ and jobs/, and each of its runs: a run with results keeps them (below),
# any other run goes. Once nothing but logs/ and manifest.json is left, the folder goes.
#
# Temp/<ucinetid>: a "unit" is one <kind>/<entry> dir (e.g. analysis/8f2c1d, scratch/runcode). A unit
# is deleted only when its ENTIRE subtree has gone untouched for --ttl-days, so a long-running job —
# which keeps writing — can never be swept out from under itself.
#
# A run's results are never swept (issue 6). A cold run with a result/ folder that has a file in it
# keeps that folder and loses everything else (its work/ checkpoints, its artifacts/ copy, its job
# files): the gateway copies result/ to the server at the owner's next connect, checks it, and only
# then removes the folder (aiscientist_session_clean.sh --verify-dir).
#
# --retire-legacy (with --user) finishes the retired layout: once Temp/<user> is empty it removes
# pysrc/<user> and bin/<user>, and uploads/<user> when nothing but empty folders is left in it. The
# gateway passes it only when no job of the user could still be reading them.
#
# Personal lab dirs (/dfs3b/ruic20_lab/<ucinetid>/) are strictly off-limits.
#
# Usage:
#   aiscientist_temp_gc.sh --root /dfs3b/ruic20_lab/software/AiScientist [--ttl-days 3]
#                          [--user <ucinetid>] [--keep-session <id>]... [--retire-legacy]
#                          [--dry-run] [--quiet]
#
#   --user     restrict the Temp sweep to one member's subtree (what the gateway passes — it sweeps
#              as, and only for, the logged-in user). Session folders are always only the caller's.
#   --dry-run  print what would be removed and remove nothing.
set -uo pipefail

ROOT=""
TTL=3
ONLY_USER=""
DRY=0
QUIET=0
KEEP=()
RETIRE=0

die() { echo "aiscientist_temp_gc: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --root)     ROOT="${2-}"; shift 2 ;;
        --ttl-days) TTL="${2-}"; shift 2 ;;
        --user)     ONLY_USER="${2-}"; shift 2 ;;
        --keep-session) KEEP+=("${2-}"); shift 2 ;;
        --retire-legacy) RETIRE=1; shift ;;
        --dry-run)  DRY=1; shift ;;
        --quiet)    QUIET=1; shift ;;
        *)          die "unknown argument: $1" ;;
    esac
done

# --- guards: refuse anything that isn't an absolute, traversal-free <root>/Temp -------------
case "$ROOT" in
    /*) ;;
    *)  die "--root must be an absolute path (got '${ROOT}')" ;;
esac
case "$ROOT" in
    *..*) die "--root must not contain '..'" ;;
esac
[ "$ROOT" = "/" ] && die "--root must not be /"
case "$TTL" in
    ''|*[!0-9]*) die "--ttl-days must be a non-negative integer (got '${TTL}')" ;;
esac
[ "$TTL" -eq 0 ] && { [ "$QUIET" -eq 1 ] || echo "ttl-days=0 — sweeping disabled, nothing to do."; exit 0; }
case "$ONLY_USER" in
    *[!a-zA-Z0-9_-]*) die "--user must be a bare account name (got '${ONLY_USER}')" ;;
esac

for k in ${KEEP[@]+"${KEEP[@]}"}; do
    case "$k" in
        ''|*[!a-zA-Z0-9_-]*) die "--keep-session must be a plain name (got '${k}')" ;;
    esac
done
[ "$RETIRE" -eq 0 ] || [ -n "$ONLY_USER" ] || die "--retire-legacy needs --user"

TEMP="${ROOT%/}/Temp"
SESSIONS="${ROOT%/}/sessions"

# Cutoff as a real file so we compare with -newer (portable) rather than parsing date strings.
STAMP="$(mktemp)" || die "could not create the cutoff stamp file"
trap 'rm -f "$STAMP"' EXIT
touch -d "-${TTL} days" "$STAMP" 2>/dev/null || touch -t "$(date -d "-${TTL} days" +%Y%m%d%H%M.%S)" "$STAMP" \
    || die "could not stamp the ${TTL}-day cutoff"

removed=0
kept=0
held=0

# True when SOMETHING in the subtree (including the dir itself) is newer than the cutoff.
is_warm() {
    [ -n "$(find "$1" -newer "$STAMP" -print -quit 2>/dev/null)" ]
}

# True when <unit>/result is a real folder with at least one file in it.
has_results() {
    [ -d "$1/result" ] && [ ! -L "$1/result" ] \
        && [ -n "$(find "$1/result" -type f -print 2>/dev/null | head -n 1)" ]
}

sweep_unit() {
    local unit="$1" child
    if is_warm "$unit"; then
        kept=$((kept + 1))
        return
    fi
    if has_results "$unit"; then
        # Results wait for the server; everything beside them goes.
        for child in "$unit"/* "$unit"/.[!.]* "$unit"/..?*; do
            { [ -e "$child" ] || [ -L "$child" ]; } || continue
            [ "$child" = "$unit/result" ] && continue
            if [ "$DRY" -eq 1 ]; then
                echo "would remove: ${child}"
            else
                rm -rf -- "$child" || echo "aiscientist_temp_gc: could not remove ${child}" >&2
            fi
        done
        [ "$QUIET" -eq 1 ] || echo "held until copied to the server: ${unit}/result"
        held=$((held + 1))
        return
    fi
    if [ "$DRY" -eq 1 ]; then
        echo "would remove: ${unit}"
    else
        rm -rf -- "$unit" || { echo "aiscientist_temp_gc: could not remove ${unit}" >&2; return; }
        [ "$QUIET" -eq 1 ] || echo "removed: ${unit}"
    fi
    removed=$((removed + 1))
}

remove_path() {
    if [ "$DRY" -eq 1 ]; then
        echo "would remove: $1"
    elif [ -d "$1" ] && [ ! -L "$1" ]; then
        rm -rf -- "$1" || echo "aiscientist_temp_gc: could not remove $1" >&2
    else
        rm -f -- "$1" || echo "aiscientist_temp_gc: could not remove $1" >&2
    fi
}

# One run of the session layout: sessions/<id>/runs/<run_id>/<kind>/... A run whose kinds hold no
# result goes whole; one with results keeps each kind's result/ and loses the rest.
sweep_session_run() {
    local run="$1" part any=0
    for part in "$run"/*/; do
        [ -d "$part" ] && has_results "${part%/}" && any=1
    done
    if [ "$any" -eq 0 ]; then
        remove_path "$run"
        [ "$QUIET" -eq 1 ] || [ "$DRY" -eq 1 ] || echo "removed: ${run}"
        removed=$((removed + 1))
        return
    fi
    for part in "$run"/* "$run"/.[!.]* "$run"/..?*; do
        { [ -e "$part" ] || [ -L "$part" ]; } || continue
        if [ -d "$part" ] && [ ! -L "$part" ] && has_results "$part"; then
            for child in "$part"/* "$part"/.[!.]* "$part"/..?*; do
                { [ -e "$child" ] || [ -L "$child" ]; } || continue
                [ "$child" = "$part/result" ] || remove_path "$child"
            done
        else
            remove_path "$part"
        fi
    done
    [ "$QUIET" -eq 1 ] || echo "held until copied to the server: ${run}"
    held=$((held + 1))
}

is_kept_session() {
    local k
    for k in ${KEEP[@]+"${KEEP[@]}"}; do
        [ "$k" = "$1" ] && return 0
    done
    return 1
}

sweep_session() {
    local s="$1" child run left
    [ -d "$s" ] && [ ! -L "$s" ] && [ -O "$s" ] || return
    is_kept_session "${s##*/}" && return
    if is_warm "$s"; then
        kept=$((kept + 1))
        return
    fi
    if [ -d "$s/runs" ] && [ ! -L "$s/runs" ]; then
        for run in "$s/runs"/*/; do
            [ -d "$run" ] || continue
            sweep_session_run "${run%/}"
        done
        [ "$DRY" -eq 1 ] || rmdir "$s/runs" 2>/dev/null
    fi
    for child in "$s"/* "$s"/.[!.]* "$s"/..?*; do
        { [ -e "$child" ] || [ -L "$child" ]; } || continue
        case "${child##*/}" in
            runs|logs|manifest.json) continue ;;
        esac
        remove_path "$child"
    done
    # Nothing but its logs and manifest left: the session is over, and so is its folder.
    left=0
    for child in "$s"/* "$s"/.[!.]* "$s"/..?*; do
        { [ -e "$child" ] || [ -L "$child" ]; } || continue
        case "${child##*/}" in
            logs|manifest.json) ;;
            *) left=1 ;;
        esac
    done
    if [ "$left" -eq 0 ]; then
        remove_path "$s"
        [ "$QUIET" -eq 1 ] || [ "$DRY" -eq 1 ] || echo "removed: ${s}"
        removed=$((removed + 1))
    fi
}

sweep_user() {
    local userdir="$1" kind entry had_child
    for kind in "$userdir"/*/; do
        [ -d "$kind" ] || continue                 # unexpanded glob / not a dir
        kind="${kind%/}"
        had_child=0
        for entry in "$kind"/*/; do
            [ -d "$entry" ] || continue
            had_child=1
            sweep_unit "${entry%/}"
        done
        if [ "$had_child" -eq 0 ]; then
            # a flat kind dir (job scripts + logs, no per-run subdirs) — age it as one unit
            sweep_unit "$kind"
        elif [ "$DRY" -eq 1 ]; then
            find "$kind" -maxdepth 1 -type f ! -newer "$STAMP" -printf 'would remove: %p\n' 2>/dev/null
        else
            # loose files sitting beside the per-run dirs (stray Slurm logs)
            find "$kind" -maxdepth 1 -type f ! -newer "$STAMP" -delete 2>/dev/null
        fi
    done
    # collapse the dirs the sweep just emptied (never the user dir itself, so perms/setgid stay)
    [ "$DRY" -eq 1 ] || find "$userdir" -mindepth 1 -type d -empty -delete 2>/dev/null
}

if [ -d "$SESSIONS" ] && [ ! -L "$SESSIONS" ]; then
    for d in "$SESSIONS"/*/; do
        [ -d "$d" ] || continue
        sweep_session "${d%/}"
    done
fi

if [ -d "$TEMP" ]; then
    if [ -n "$ONLY_USER" ]; then
        [ -d "${TEMP}/${ONLY_USER}" ] && sweep_user "${TEMP}/${ONLY_USER}"
    else
        for d in "$TEMP"/*/; do
            [ -d "$d" ] || continue
            sweep_user "${d%/}"
        done
    fi
fi

# The retired layout, once nothing of it is in use: Temp/<user> empty, then pysrc/ and bin/.
retired=0
if [ "$RETIRE" -eq 1 ] && [ "$DRY" -eq 0 ]; then
    t="${TEMP}/${ONLY_USER}"
    if [ ! -e "$t" ] || rmdir "$t" 2>/dev/null; then
        for d in "${ROOT%/}/pysrc/${ONLY_USER}" "${ROOT%/}/bin/${ONLY_USER}"; do
            if [ -d "$d" ] && [ ! -L "$d" ] && [ -O "$d" ]; then
                rm -rf -- "$d" && { [ "$QUIET" -eq 1 ] || echo "retired: ${d}"; retired=$((retired + 1)); }
            fi
        done
        u="${ROOT%/}/uploads/${ONLY_USER}"
        if [ -d "$u" ] && [ ! -L "$u" ] && [ -O "$u" ] && [ -z "$(find "$u" ! -type d -print 2>/dev/null | head -n 1)" ]; then
            find "$u" -depth -type d -empty -delete 2>/dev/null
            [ -e "$u" ] || { [ "$QUIET" -eq 1 ] || echo "retired: ${u}"; retired=$((retired + 1)); }
        fi
    fi
fi

[ "$QUIET" -eq 1 ] || echo "aiscientist_temp_gc: removed=${removed} kept=${kept} held=${held} retired=${retired} ttl=${TTL}d root=${ROOT%/}${ONLY_USER:+ user=${ONLY_USER}}"
# Machine-readable last line the gateway parses.
echo "GC_RESULT removed=${removed} kept=${kept} held=${held} retired=${retired}"
