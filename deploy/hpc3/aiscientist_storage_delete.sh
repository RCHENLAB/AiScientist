#!/bin/bash
# AiScientist: delete what a user chose in the console's Storage tab, as a Slurm job.
#
# The Storage tab lists the user's own HPC3 folders, and its Delete button removes one entry. The
# gateway submits this script with sbatch instead of running rm on a login node: removing data is
# filesystem work, which RCIC keeps off the login nodes. It deletes each --path it is given when
#   * the path is strictly inside one of the user's own folders: <root>/Temp/<user>/,
#     <root>/uploads/<user>/, a session folder <root>/sessions/<id> the user owns (the folder
#     itself, or anything in it) or, when --personal names it, the personal lab folder (which must
#     end in /<user>); never one of the other folders itself;
#   * the path has no '..', '.' or empty component, and no symbolic link above it: the physical
#     folder it is in must be the one named.
# A path that is itself a symbolic link is removed as a link; what it points to is not touched.
# Inside a folder, rm -rf removes links without following them. Anything else is skipped, with the
# reason.
#
# It runs only inside a Slurm job (SLURM_JOB_ID set). The gateway submits it with sbatch.
#
# Usage:
#   aiscientist_storage_delete.sh --root <shared_root> --user <ucinetid>
#                                 [--personal <lab folder>/<ucinetid>] --path <path> [--path <path>]...
#
# Output, tab-separated, one line per path, then a summary line:
#   DELETED <path>
#   SKIP    <reason> <path>
#   DELETE_RESULT deleted=N skipped=N job=<id>
set -uo pipefail

ROOT=""
ONLY_USER=""
PERSONAL=""
PATHS=()

die() { echo "aiscientist_storage_delete: $*" >&2; exit 2; }

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
        --personal) PERSONAL="${2-}"; shift 2 ;;
        --path)     PATHS+=("${2-}"); shift 2 ;;
        *)          die "unknown argument: $1" ;;
    esac
done

[ -n "${SLURM_JOB_ID:-}" ] || die "run this as a Slurm job (sbatch), never on a login node"
clean_abs "$ROOT" || die "--root must be a clean absolute path (got '${ROOT}')"
[ "$ROOT" = "/" ] && die "--root must not be /"
is_name "$ONLY_USER" || die "--user must be a bare account name (got '${ONLY_USER}')"
if [ -n "$PERSONAL" ]; then
    clean_abs "$PERSONAL" || die "--personal must be a clean absolute path (got '${PERSONAL}')"
    [ "${PERSONAL##*/}" = "$ONLY_USER" ] || die "--personal must be the folder of ${ONLY_USER} (got '${PERSONAL}')"
fi
[ "${#PATHS[@]}" -gt 0 ] || die "nothing to delete: give at least one --path"

TEMP="${ROOT%/}/Temp/${ONLY_USER}"
UPLOADS="${ROOT%/}/uploads/${ONLY_USER}"
SESSIONS="${ROOT%/}/sessions"

deleted=0; skipped=0

skip() {
    printf 'SKIP\t%s\t%s\n' "$1" "$2"
    skipped=$((skipped + 1))
}

for path in "${PATHS[@]}"; do
    case "$path" in
        *$'\n'*|*$'\t'*|*$'\r'*) skip "a control character in its name" "$path"; continue ;;
    esac
    if ! clean_abs "$path"; then
        skip "not a clean absolute path" "$path"; continue
    fi
    inside=0
    case "$path" in
        "$TEMP"/?*|"$UPLOADS"/?*) inside=1 ;;
    esac
    if [ "$inside" -eq 0 ]; then
        # A session folder, or anything in one, of a session folder this user owns.
        case "$path" in
            "$SESSIONS"/?*)
                sid="${path#"$SESSIONS"/}"
                sid="${sid%%/*}"
                if is_name "$sid" && [ -d "$SESSIONS/$sid" ] && [ ! -L "$SESSIONS/$sid" ] && [ -O "$SESSIONS/$sid" ]; then
                    inside=1
                fi
                ;;
        esac
    fi
    if [ "$inside" -eq 0 ] && [ -n "$PERSONAL" ]; then
        case "$path" in
            "$PERSONAL"/?*) inside=1 ;;
        esac
    fi
    if [ "$inside" -eq 0 ]; then
        skip "outside the user's own folders" "$path"; continue
    fi
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then
        skip "not there" "$path"; continue
    fi
    # No link anywhere above it: the physical directory must be the one named.
    parent="${path%/*}"
    phys="$(cd -- "$parent" 2>/dev/null && pwd -P)"
    if [ "$phys" != "$parent" ]; then
        skip "a symbolic link on the way to it" "$path"; continue
    fi
    if [ -L "$path" ] || [ ! -d "$path" ]; then
        rm -f -- "$path"             # a file, or a link itself (never what it points to)
    else
        rm -rf -- "$path"
    fi
    if [ -e "$path" ] || [ -L "$path" ]; then
        skip "could not be deleted (permissions?)" "$path"; continue
    fi
    printf 'DELETED\t%s\n' "$path"
    deleted=$((deleted + 1))
done

echo "DELETE_RESULT deleted=${deleted} skipped=${skipped} job=${SLURM_JOB_ID}"
