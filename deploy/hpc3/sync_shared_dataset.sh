#!/bin/bash
# Copy one published shared-dataset version to HPC3, check every checksum there, then make it visible.
#
# A shared dataset (RCHENLAB/AiScientist issue 92, gateway/shared_datasets.py) is published on the
# gateway host into AISCIENTIST_SHARED_DATASETS_ROOT/<slug>/<label>/. HPC3 keeps ONE read-only copy
# of each version, which runs read in place (no per-session copy):
#
#   <root>/datasets/<slug>/<label>/       files 0444, folders 0555, never swept
#   <root>/datasets/<slug>/latest -> ...  follows the gateway store's latest
#
# Steps, as the HPC3 account given with --user (its SSH key must work for both hosts):
#   1. rsync the version through the transfer node (RCIC forbids transfers on login nodes) into
#      .<label>.partial/, hard-linking files unchanged since the version HPC3's latest points at
#      (Linux lets an account hard-link only files it owns: when another account synced that
#      version, the unchanged files are copied instead, which costs space, not correctness);
#   2. a Slurm job on a compute node runs `sha256sum -c SHA256SUMS` on the copy (sbatch --wait);
#   3. only then: rename to <label>, make it read-only, move latest when the store's latest is <label>.
# Then record it on the gateway host, as the service account:
#   aiscientist-admin shared-dataset hpc-verified <slug> <label>
#
# Usage (on the gateway host):
#   deploy/hpc3/sync_shared_dataset.sh --store /data/aiscientist/shared-datasets --slug heca \
#       --label v2026.11 --user <ucinetid> [--root /dfs3b/ruic20_lab/software/AiScientist]
#       [--login hpc3.rcic.uci.edu] [--transfer access-hpc3.rcic.uci.edu]
#       [--partition standard] [--account ruic20_lab] [--previous <label>] [--dry-run]
#
# Re-running after a failure starts the copy over from the partial folder (rsync skips what is
# already there). A version already on HPC3 is refused.
set -euo pipefail

STORE="" SLUG="" LABEL="" HPC_USER="" PREVIOUS="" DRY=0
ROOT="/dfs3b/ruic20_lab/software/AiScientist"
LOGIN="hpc3.rcic.uci.edu" TRANSFER="access-hpc3.rcic.uci.edu"
PARTITION="standard" ACCOUNT="ruic20_lab"

die() { echo "sync_shared_dataset: $*" >&2; exit 1; }
while [ $# -gt 0 ]; do
  case "$1" in
    --store) STORE="$2"; shift 2 ;;
    --slug) SLUG="$2"; shift 2 ;;
    --label) LABEL="$2"; shift 2 ;;
    --user) HPC_USER="$2"; shift 2 ;;
    --root) ROOT="${2%/}"; shift 2 ;;
    --login) LOGIN="$2"; shift 2 ;;
    --transfer) TRANSFER="$2"; shift 2 ;;
    --partition) PARTITION="$2"; shift 2 ;;
    --account) ACCOUNT="$2"; shift 2 ;;
    --previous) PREVIOUS="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *) die "unknown option $1 (see --help)" ;;
  esac
done
[ -n "$STORE" ] && [ -n "$SLUG" ] && [ -n "$LABEL" ] && [ -n "$HPC_USER" ] || die "need --store, --slug, --label and --user"
[[ "$SLUG" =~ ^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$ ]] || die "invalid slug $SLUG"
[[ "$LABEL" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$ ]] && [ "$LABEL" != latest ] && [[ "$LABEL" != *..* ]] || die "invalid label $LABEL"
[[ -z "$PREVIOUS" || "$PREVIOUS" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$ ]] || die "invalid --previous $PREVIOUS"

SRC="$STORE/$SLUG/$LABEL"
[ -f "$SRC/manifest.json" ] && [ -f "$SRC/SHA256SUMS" ] || die "$SRC is not a published version (no manifest.json / SHA256SUMS)"
DEST_PARENT="$ROOT/datasets/$SLUG"
PARTIAL="$DEST_PARENT/.$LABEL.partial"
FINAL="$DEST_PARENT/$LABEL"
STORE_LATEST="$(readlink "$STORE/$SLUG/latest" 2>/dev/null || true)"
SSH=(ssh -o BatchMode=yes -o ServerAliveInterval=60)
q() { printf '%q' "$1"; }
run() { echo "+ $*"; if [ "$DRY" = 0 ]; then "$@"; fi; }
on_login() { run "${SSH[@]}" "$HPC_USER@$LOGIN" "$1"; }

if [ "$DRY" = 0 ]; then
  if "${SSH[@]}" "$HPC_USER@$LOGIN" "test -e $(q "$FINAL")"; then
    die "$FINAL already exists on HPC3: a version is never replaced"
  fi
  if [ -z "$PREVIOUS" ]; then
    PREVIOUS="$("${SSH[@]}" "$HPC_USER@$LOGIN" "readlink $(q "$DEST_PARENT/latest") 2>/dev/null || true")"
  fi
fi

# 1. Folders (a tiny command on the login node), then the copy through the transfer node.
#    datasets/ and datasets/<slug>/ are group-writable with setgid, so any maintainer can sync.
on_login "umask 002 && mkdir -p $(q "$DEST_PARENT") && chmod 2775 $(q "$ROOT/datasets") $(q "$DEST_PARENT") 2>/dev/null; mkdir -p $(q "$PARTIAL")"
LINK=()
if [ -n "$PREVIOUS" ] && [ "$PREVIOUS" != "$LABEL" ]; then
  LINK=(--link-dest="../$PREVIOUS")
  echo "files unchanged since $PREVIOUS are hard-linked to it"
fi
run rsync -a --no-o --no-g --chmod=D2775,F0444 --info=progress2 ${LINK[@]+"${LINK[@]}"} \
  -e "ssh -o BatchMode=yes -o ServerAliveInterval=60" "$SRC/" "$HPC_USER@$TRANSFER:$PARTIAL/"

# 2. Every checksum, on a compute node. sbatch --wait returns the job's exit status.
VERIFY_LOG="$DEST_PARENT/.$LABEL.verify.log"
JOB="cd $(q "$PARTIAL") && sha256sum --quiet --strict -c SHA256SUMS && echo VERIFIED"
on_login "sbatch --wait --parsable -J aiscientist-dataset-verify -p $(q "$PARTITION") ${ACCOUNT:+-A $(q "$ACCOUNT")} -c 2 --mem=2G -t 04:00:00 -o $(q "$VERIFY_LOG") --wrap $(q "$JOB")"
if [ "$DRY" = 0 ]; then
  "${SSH[@]}" "$HPC_USER@$LOGIN" "grep -qx VERIFIED $(q "$VERIFY_LOG")" \
    || die "checksums differ on HPC3: see $VERIFY_LOG; the copy stays in $PARTIAL"
fi

# 3. Visible: rename, read-only, and latest when the store's latest is this version.
MOVE_LATEST=""
if [ "$STORE_LATEST" = "$LABEL" ]; then
  MOVE_LATEST=" && ln -sfn $(q "$LABEL") .latest.tmp && mv -Tf .latest.tmp latest"
fi
on_login "cd $(q "$DEST_PARENT") && mv -T $(q ".$LABEL.partial") $(q "$LABEL") && find $(q "$LABEL") -depth -type d -exec chmod 0555 {} +$MOVE_LATEST"

echo "HPC3 copy verified: $FINAL"
[ -n "$MOVE_LATEST" ] && echo "HPC3 latest -> $LABEL"
echo "Now record it on this host, as the service account:"
echo "  aiscientist-admin shared-dataset hpc-verified $SLUG $LABEL"
