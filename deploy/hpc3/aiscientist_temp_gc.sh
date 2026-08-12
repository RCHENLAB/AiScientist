#!/bin/bash
# AiScientist HPC3 Temp sweeper — delete per-run process files once they go cold.
#
# Everything AiScientist generates on HPC3 lands under ONE shared project root:
#
#   <root>/Temp/<ucinetid>/<kind>/<entry>   process files   <- THIS SCRIPT DELETES THESE
#   <root>/uploads/<ucinetid>/...           raw research data     (never touched)
#   <root>/pysrc/<ucinetid>/...             synced source         (never touched)
#
# A "unit" is one <kind>/<entry> dir (e.g. analysis/8f2c1d, scratch/runcode). A unit is deleted
# only when its ENTIRE subtree has gone untouched for --ttl-days, so a long-running job — which
# keeps writing — can never be swept out from under itself.
#
# Nothing outside <root>/Temp is ever considered: personal lab dirs
# (/dfs3b/ruic20_lab/<ucinetid>/) and uploads are strictly off-limits.
#
# Usage:
#   aiscientist_temp_gc.sh --root /dfs3b/ruic20_lab/AiScientist [--ttl-days 3]
#                          [--user <ucinetid>] [--dry-run] [--quiet]
#
#   --user     restrict to one member's Temp subtree (what the gateway passes — it sweeps as,
#              and only for, the logged-in user). Omit to sweep every member's.
#   --dry-run  print what would be removed and remove nothing.
#
# Cron backstop (login node; the sweep is a handful of stat calls, not a data transfer):
#   0 3 * * * /dfs3b/ruic20_lab/AiScientist/bin/temp_gc.sh \
#               --root /dfs3b/ruic20_lab/AiScientist --ttl-days 3 --user $USER --quiet
set -uo pipefail

ROOT=""
TTL=3
ONLY_USER=""
DRY=0
QUIET=0

die() { echo "aiscientist_temp_gc: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
    case "$1" in
        --root)     ROOT="${2-}"; shift 2 ;;
        --ttl-days) TTL="${2-}"; shift 2 ;;
        --user)     ONLY_USER="${2-}"; shift 2 ;;
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

TEMP="${ROOT%/}/Temp"
[ -d "$TEMP" ] || { [ "$QUIET" -eq 1 ] || echo "no Temp dir at ${TEMP} — nothing to do."; exit 0; }

# Cutoff as a real file so we compare with -newer (portable) rather than parsing date strings.
STAMP="$(mktemp)" || die "could not create the cutoff stamp file"
trap 'rm -f "$STAMP"' EXIT
touch -d "-${TTL} days" "$STAMP" 2>/dev/null || touch -t "$(date -d "-${TTL} days" +%Y%m%d%H%M.%S)" "$STAMP" \
    || die "could not stamp the ${TTL}-day cutoff"

removed=0
kept=0

# True when SOMETHING in the subtree (including the dir itself) is newer than the cutoff.
is_warm() {
    [ -n "$(find "$1" -newer "$STAMP" -print -quit 2>/dev/null)" ]
}

sweep_unit() {
    local unit="$1"
    if is_warm "$unit"; then
        kept=$((kept + 1))
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

if [ -n "$ONLY_USER" ]; then
    [ -d "${TEMP}/${ONLY_USER}" ] && sweep_user "${TEMP}/${ONLY_USER}"
else
    for d in "$TEMP"/*/; do
        [ -d "$d" ] || continue
        sweep_user "${d%/}"
    done
fi

[ "$QUIET" -eq 1 ] || echo "aiscientist_temp_gc: removed=${removed} kept=${kept} ttl=${TTL}d root=${TEMP}${ONLY_USER:+ user=${ONLY_USER}}"
# Machine-readable last line the gateway parses.
echo "GC_RESULT removed=${removed} kept=${kept}"
