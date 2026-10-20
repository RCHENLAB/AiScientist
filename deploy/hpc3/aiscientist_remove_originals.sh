#!/bin/bash
# AiScientist: remove the HPC3 originals of datasets that were copied to the server.
#
# A dataset that was kept only on HPC3 is copied to the server when its owner connects; the copy's
# source.json names the HPC3 original, which is kept. This script removes such originals, and only
# those listed in the manifest it is given, one per line:
#
#   <file|dir> <TAB> <bytes> <TAB> <absolute path>
#
# where <bytes> is the size of the copy on the server. An original is removed only when
#   * it is strictly inside the user's uploads folder, <root>/uploads/<user>/, or, when --personal
#     names it, strictly inside the user's personal lab folder (which must end in /<user>);
#   * its path has no '..', and no symbolic link anywhere on it;
#   * it is what the manifest says (a file or a folder), a folder holds no link and no hidden file
#     (neither was copied to the server), and its size here is exactly <bytes> (for a folder, the
#     sum of its files).
# Anything else is skipped, with the reason. --dry-run removes nothing and says what it would.
#
# It runs only inside a Slurm job (SLURM_JOB_ID set): removing data is filesystem work, which RCIC
# keeps off the login nodes. The gateway submits it with sbatch.
#
# Usage:
#   aiscientist_remove_originals.sh --root <shared_root> --user <ucinetid> --manifest <file>
#                                   [--personal <lab folder>/<ucinetid>] [--dry-run]
#
# Output, tab-separated, one line per original, then a summary line:
#   WOULD_REMOVE <bytes> <path>        (dry run)
#   REMOVED      <bytes> <path>
#   SKIP         <reason> <path>
#   ORIGINALS_RESULT checked=N would_remove=N removed=N skipped=N bytes=N dry_run=0|1 job=<id>
set -uo pipefail

ROOT=""
ONLY_USER=""
MANIFEST=""
PERSONAL=""
DRY=0

die() { echo "aiscientist_remove_originals: $*" >&2; exit 2; }

is_name() {
    case "$1" in
        ''|*[!a-zA-Z0-9_-]*) return 1 ;;
    esac
    return 0
}

# An absolute path with no '..', '.', empty or trailing component.
clean_abs() {
    case "$1" in
        /*) ;;
        *) return 1 ;;
    esac
    case "$1" in
        */|*//*|*/./*|*/.|*/../*|*/..) return 1 ;;
    esac
    return 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --root)     ROOT="${2-}"; shift 2 ;;
        --user)     ONLY_USER="${2-}"; shift 2 ;;
        --manifest) MANIFEST="${2-}"; shift 2 ;;
        --personal) PERSONAL="${2-}"; shift 2 ;;
        --dry-run)  DRY=1; shift ;;
        *)          die "unknown argument: $1" ;;
    esac
done

[ -n "${SLURM_JOB_ID:-}" ] || die "run this as a Slurm job (sbatch), never on a login node"
clean_abs "$ROOT" || die "--root must be a clean absolute path (got '${ROOT}')"
[ "$ROOT" = "/" ] && die "--root must not be /"
is_name "$ONLY_USER" || die "--user must be a bare account name (got '${ONLY_USER}')"
[ -f "$MANIFEST" ] || die "--manifest must be a file (got '${MANIFEST}')"
if [ -n "$PERSONAL" ]; then
    clean_abs "$PERSONAL" || die "--personal must be a clean absolute path (got '${PERSONAL}')"
    [ "${PERSONAL##*/}" = "$ONLY_USER" ] || die "--personal must be the folder of ${ONLY_USER} (got '${PERSONAL}')"
fi

UPLOADS="${ROOT%/}/uploads/${ONLY_USER}"

file_bytes() { stat -c %s -- "$1" 2>/dev/null || stat -f %z -- "$1" 2>/dev/null; }

dir_bytes() {
    local out
    if out="$(find "$1" -type f -exec stat -c %s {} + 2>/dev/null)"; then :
    elif out="$(find "$1" -type f -exec stat -f %z {} + 2>/dev/null)"; then :
    else return 1
    fi
    printf '%s\n' "$out" | awk '{ s += $1 } END { printf "%d\n", s + 0 }'
}

checked=0; would=0; removed=0; skipped=0; total=0

skip() {
    printf 'SKIP\t%s\t%s\n' "$1" "$2"
    skipped=$((skipped + 1))
}

while IFS=$'\t' read -r kind bytes path || [ -n "${kind:-}" ]; do
    [ -n "${kind:-}" ] || continue
    checked=$((checked + 1))
    case "$kind" in
        file|dir) ;;
        *) skip "unknown kind ${kind}" "${path:-}"; continue ;;
    esac
    case "${bytes:-}" in
        ''|*[!0-9]*) skip "no size" "${path:-}"; continue ;;
    esac
    if ! clean_abs "${path:-}"; then
        skip "not a clean absolute path" "${path:-}"; continue
    fi
    base=""
    case "$path" in
        "$UPLOADS"/?*) base="$UPLOADS" ;;
    esac
    if [ -z "$base" ] && [ -n "$PERSONAL" ]; then
        case "$path" in
            "$PERSONAL"/?*) base="$PERSONAL" ;;
        esac
    fi
    if [ -z "$base" ]; then
        skip "outside the user's own folders" "$path"; continue
    fi
    if [ -L "$path" ]; then
        skip "a symbolic link" "$path"; continue
    fi
    if [ ! -e "$path" ]; then
        skip "not there" "$path"; continue
    fi
    # No link anywhere above it either: the physical directory must be the one named.
    parent="${path%/*}"
    phys="$(cd -- "$parent" 2>/dev/null && pwd -P)"
    if [ "$phys" != "$parent" ]; then
        skip "a symbolic link on the way to it" "$path"; continue
    fi
    if [ "$kind" = "file" ]; then
        if [ ! -f "$path" ]; then skip "not a file" "$path"; continue; fi
        have="$(file_bytes "$path")"
    else
        if [ ! -d "$path" ]; then skip "not a folder" "$path"; continue; fi
        if [ -n "$(find "$path" -type l -print 2>/dev/null | head -n 1)" ]; then
            skip "holds a symbolic link, which was not copied" "$path"; continue
        fi
        if [ -n "$(find "$path" -mindepth 1 -name '.*' -print 2>/dev/null | head -n 1)" ]; then
            skip "holds hidden files, which were not copied" "$path"; continue
        fi
        have="$(dir_bytes "$path")"
    fi
    if [ "${have:-x}" != "$bytes" ]; then
        skip "size ${have:-unknown} here, ${bytes} on the server" "$path"; continue
    fi
    if [ "$DRY" -eq 1 ]; then
        printf 'WOULD_REMOVE\t%s\t%s\n' "$bytes" "$path"
        would=$((would + 1))
        total=$((total + bytes))
        continue
    fi
    if [ "$kind" = "dir" ]; then
        rm -rf -- "$path"
    else
        rm -f -- "$path"
    fi
    if [ -e "$path" ]; then
        skip "could not be removed" "$path"; continue
    fi
    printf 'REMOVED\t%s\t%s\n' "$bytes" "$path"
    removed=$((removed + 1))
    total=$((total + bytes))
done < "$MANIFEST"

echo "ORIGINALS_RESULT checked=${checked} would_remove=${would} removed=${removed} skipped=${skipped} bytes=${total} dry_run=${DRY} job=${SLURM_JOB_ID}"
