#!/usr/bin/env bash
# Move the production deployment root (default /data/BioAgent -> /data/aiscientist) and rename the
# systemd unit (bioagent.service -> aiscientist.service). One-off, run ON the server as root:
#
#     scp scripts/migrate_prod_root.sh eyeserver-admin:/tmp/
#     ssh -t eyeserver-admin 'sudo bash /tmp/migrate_prod_root.sh'            # preflight only
#     ssh -t eyeserver-admin 'sudo bash /tmp/migrate_prod_root.sh --apply'    # do it
#     ssh -t eyeserver-admin 'sudo bash /tmp/migrate_prod_root.sh --rollback' # undo --apply
#     ssh -t eyeserver-admin 'sudo bash /tmp/migrate_prod_root.sh --harden'   # optional, later
#
# Why a bare `mv` is not enough (all measured on the prod host, 2026-10-09):
#   * the venv is an EDITABLE install: its .pth names <old>/app/src, biomni's finder maps
#     <old>/biomni/..., and every console script's shebang is <old>/env/bin/python. After a bare mv
#     `import aiscientist` fails and systemd restarts the service in a loop.
#   * Postgres `datasets.path` and the runs' run_state.json / result_dataset.json keep absolute
#     <old>/... paths that the app does NOT re-derive (old dataset -> "Not bound", continuing an old
#     run fails, "Save to my data" -> 409).
#   * the passwordless restart grant (and any other rule) in /etc/sudoers.d names `bioagent.service`.
# So --apply: stops the service, moves the tree, leaves a compatibility symlink <old> -> <new>,
# rewrites the old prefix in the venv, .env, the database rows and the runs' JSON, installs the
# renamed unit and sudoers grant, starts and checks the service. Every file it edits is backed up
# first under <new>/env-backups/root-move-<stamp>/, which is what --rollback restores from.
#
# Nothing on HPC3 changes (Slurm job names, ~/.bioagent, software/AiScientist stay as they are).
#
# Rehearse before running it for real, and again after any change here: deploy/rehearsal/run.sh
# runs every mode as root in a container against a miniature of the host and asserts the outcome.
set -euo pipefail

OLD="${OLD:-/data/BioAgent}"
NEW="${NEW:-/data/aiscientist}"
SVC_USER="${SVC_USER:-aiscientist}"
OLD_UNIT="${OLD_UNIT:-bioagent.service}"
NEW_UNIT="${NEW_UNIT:-aiscientist.service}"
BIND_HOST="${BIND_HOST:-128.200.71.197}"
PORT="${PORT:-8800}"
IDLE_MINUTES="${IDLE_MINUTES:-5}"
OLD_SUDOERS="${OLD_SUDOERS:-/etc/sudoers.d/bioagent-deploy}"
NEW_SUDOERS="${NEW_SUDOERS:-/etc/sudoers.d/aiscientist-deploy}"
STAMP="$(date +%Y%m%d-%H%M%S)"

MODE="check"
case "${1:-}" in
  "") ;;
  --apply) MODE=apply ;;
  --rollback) MODE=rollback ;;
  --harden) MODE=harden ;;
  -h|--help) sed -n '2,26p' "$0"; exit 0 ;;
  *) echo "unknown option: $1 (use --apply, --rollback, --harden or nothing)" >&2; exit 2 ;;
esac

say()  { printf '\n==> %s\n' "$*"; }
ok()   { printf '    ok: %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*"; }
die()  { printf '\nABORT: %s\n' "$*" >&2; exit 1; }
as_svc() { (cd /tmp && sudo -u "$SVC_USER" "$@"); }

[ "$(id -u)" -eq 0 ] || die "run as root (sudo bash $0 ...)"
id "$SVC_USER" >/dev/null 2>&1 || die "service account $SVC_USER does not exist"

unit_text() {   # the renamed unit, with the new root
  cat <<EOF
# AiScientist console (FastAPI/uvicorn). Installed by scripts/migrate_prod_root.sh on ${STAMP}.
# The repository template is deploy/systemd/aiscientist.service.
[Unit]
Description=AiScientist console (FastAPI/uvicorn)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=${SVC_USER}
Group=${SVC_USER}
WorkingDirectory=${NEW}/app
EnvironmentFile=-${NEW}/app/.env
ExecStart=${NEW}/env/bin/python -m aiscientist.gateway --host ${BIND_HOST} --port ${PORT}
Restart=always
RestartSec=2
TimeoutStopSec=20
KillSignal=SIGINT

[Install]
WantedBy=multi-user.target
EOF
}

# Rewrite rows that store the old root. Writes the old values first, so --rollback can restore them.
db_rewrite() {   # $1 = from-prefix, $2 = to-prefix, $3 = backup json (written when $4 = save)
  as_svc env PYTHONPATH="$NEW/app/src" "$NEW/env/bin/python" - "$1" "$2" "$3" "${4:-}" <<'PY'
import json, os, sys
from pathlib import Path
src, dst, backup, save = sys.argv[1:5]
from aiscientist.core.config import load_project_env
load_project_env(Path(os.environ["PYTHONPATH"]).parent)
from sqlalchemy import create_engine, text
eng = create_engine(os.environ["AISCIENTIST_DATABASE_URL"])
COLS = (("datasets", "path"), ("runs", "artifacts_path"))
with eng.begin() as c:
    if save:
        rows = {f"{t}.{col}": [list(r) for r in c.execute(text(
            f'select id, "{col}" from "{t}" where "{col}" like :p'), {"p": src + "/%"})]
            for t, col in COLS}
        Path(backup).write_text(json.dumps(rows, indent=1))
    for t, col in COLS:
        n = c.execute(text(f'update "{t}" set "{col}" = :dst || substr("{col}", :k) '
                           f'where "{col}" like :p'),
                      {"dst": dst, "k": len(src) + 1, "p": src + "/%"}).rowcount
        print(f"    {t}.{col}: {n} rows {src} -> {dst}")
PY
}

# Every run JSON under $3 that names the old prefix. Rewritten IN PLACE with its mtime put back:
# the same inode, owner, mode and ACL, and a run that finished weeks ago must not look freshly
# written (the idle check above and the checkpoint/Temp expiry both go by mtime).
json_rewrite() { # $1 = from, $2 = to, $3 = where to look
  { grep -rlZ --include='*.json' -F "$1/" "$3" 2>/dev/null || true; } | as_svc python3 -c '
import os, sys
src, dst = (sys.argv[1] + "/").encode(), (sys.argv[2] + "/").encode()
names = [n for n in sys.stdin.buffer.read().split(b"\0") if n]
for n in names:
    st = os.stat(n)
    with open(n, "r+b") as fh:
        data = fh.read().replace(src, dst)
        fh.seek(0); fh.write(data); fh.truncate()
    os.utime(n, ns=(st.st_atime_ns, st.st_mtime_ns))
print(f"    ok: {len(names)} run JSON files: {sys.argv[1]} -> {sys.argv[2]}")
' "$1" "$2"
}

venv_files() {   # text files in the venv that carry the given prefix (shebangs, .pth, finder, cfg)
  grep -rlI --exclude-dir=__pycache__ -F "$1" "$2/env" 2>/dev/null || true
}

sudoers_mentions() {   # file:line:text for every sudoers line that names the old unit (bare or .service)
  grep -rn -i -- "${OLD_UNIT%.service}" /etc/sudoers /etc/sudoers.d 2>/dev/null || true
}

health() {
  local code i
  for i in $(seq 1 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://${BIND_HOST}:${PORT}/" || true)"
    case "$code" in 200|302|401) ok "http://${BIND_HOST}:${PORT}/ -> $code"; return 0 ;; esac
    sleep 3
  done
  return 1
}

# ---------------------------------------------------------------------------------------------
if [ "$MODE" = "check" ] || [ "$MODE" = "apply" ]; then
  say "preflight ($OLD -> $NEW, $OLD_UNIT -> $NEW_UNIT)"
  [ -d "$OLD" ] && [ ! -L "$OLD" ] || die "$OLD is not a real directory (already moved?)"
  if [ -e "$NEW" ]; then
    [ -d "$NEW" ] && [ -z "$(ls -A "$NEW")" ] || die "$NEW exists and is not an empty directory"
    ok "$NEW exists and is empty ($(stat -c '%U:%G %a' "$NEW")) — it will be replaced by the tree"
  fi
  [ "$(stat -c %d "$OLD")" = "$(stat -c %d "$(dirname "$NEW")")" ] || die "$NEW is on another filesystem; mv would copy"
  [ -f "/etc/systemd/system/$OLD_UNIT" ] || die "/etc/systemd/system/$OLD_UNIT not found"
  [ ! -e "/etc/systemd/system/$NEW_UNIT" ] || die "/etc/systemd/system/$NEW_UNIT already exists"
  [ ! -d "/etc/systemd/system/$OLD_UNIT.d" ] || die "$OLD_UNIT has drop-ins; move them by hand first"
  [ -f "$OLD/app/.env" ] || die "$OLD/app/.env not found"
  ok "unit, .env and paths look as expected"

  echo "    .env lines that name $OLD:"; grep -n -F "$OLD" "$OLD/app/.env" | sed 's/^/      /' || echo "      (none)"
  echo "    venv files that name $OLD: $(venv_files "$OLD" "$OLD" | wc -l)"
  echo "    run JSON files that name $OLD: $(grep -rl --include='*.json' -F "$OLD/" "$OLD/app/runs" 2>/dev/null | wc -l)"
  # Every sudoers line that names the old unit, wherever it lives. Files under /etc/sudoers.d that
  # contain "$OLD_UNIT" are rewritten by --apply; anything else listed here is NOT touched.
  sud="$(sudoers_mentions)"
  if [ -n "$sud" ]; then
    echo "    sudoers lines that mention ${OLD_UNIT%.service}:"; echo "$sud" | sed 's/^/      /'
    grep -rlq -F -- "$OLD_UNIT" /etc/sudoers.d 2>/dev/null \
      || warn "none of them is a /etc/sudoers.d file naming $OLD_UNIT — the passwordless restart grant will need a hand edit"
  else
    warn "no sudoers line mentions ${OLD_UNIT%.service} — grant passwordless systemctl {start,stop,restart} $NEW_UNIT by hand afterwards"
  fi

  busy="$(find "$OLD/app/runs/console" -type f -newermt "-${IDLE_MINUTES} minutes" 2>/dev/null | head -5 || true)"
  if [ -n "$busy" ]; then
    warn "files written in the last ${IDLE_MINUTES} min (a run may be active):"; echo "$busy" | sed 's/^/      /'
    [ "$MODE" = "apply" ] && [ "${FORCE:-0}" != "1" ] && die "prod is not idle (FORCE=1 to override; a restart kills active runs)"
  else
    ok "no run wrote anything in the last ${IDLE_MINUTES} min"
  fi
  echo "    open connections to :$PORT: $(ss -tn state established "( sport = :$PORT )" 2>/dev/null | tail -n +2 | wc -l)"

  echo; echo "    unit that will be installed as /etc/systemd/system/$NEW_UNIT:"; unit_text | sed 's/^/      | /'
  if [ "$MODE" = "check" ]; then
    say "preflight only — nothing changed. Re-run with --apply."
    exit 0
  fi

  # --- apply ---------------------------------------------------------------------------------
  BK_REL="env-backups/root-move-$STAMP"
  say "1/9 stop $OLD_UNIT"
  systemctl stop "$OLD_UNIT"; ok "stopped"

  say "2/9 move $OLD -> $NEW and leave a compatibility symlink"
  [ -d "$NEW" ] && rmdir "$NEW"
  mv "$OLD" "$NEW"
  ln -s "$NEW" "$OLD"
  ok "$(ls -ld "$OLD" | awk '{print $9, $10, $11}')"
  BK="$NEW/$BK_REL"; install -d -m 0700 -o "$SVC_USER" -g "$SVC_USER" "$BK"

  say "3/9 back up everything this run edits -> $BK"
  cp -p "$NEW/app/.env" "$BK/app.env"
  cp -p "/etc/systemd/system/$OLD_UNIT" "$BK/$OLD_UNIT"
  install -d -m 0700 "$BK/sudoers.d"
  while IFS= read -r -d '' f; do cp -p "$f" "$BK/sudoers.d/"; done \
    < <(grep -rlZ -F -- "$OLD_UNIT" /etc/sudoers.d 2>/dev/null || true)
  venv_files "$OLD" "$NEW" > "$BK/venv-files.txt"
  tar -C / -cpf "$BK/venv-files.tar" --files-from=<(sed 's#^/##' "$BK/venv-files.txt")
  ok "$(wc -l < "$BK/venv-files.txt") venv files, .env, unit, sudoers"

  say "4/9 venv: rewrite $OLD -> $NEW (shebangs, editable .pth, biomni finder, pyvenv.cfg)"
  while IFS= read -r f; do as_svc sed -i -e "s#$OLD\\b#$NEW#g" "$f"; done < "$BK/venv-files.txt"
  left="$(venv_files "$OLD" "$NEW" | wc -l)"; [ "$left" -eq 0 ] || die "$left venv files still name $OLD"
  imp="$(as_svc "$NEW/env/bin/python" -c 'import aiscientist; print(aiscientist.__file__)')" \
    || die "the venv cannot import aiscientist — run --rollback"
  case "$imp" in "$NEW"/app/src/*) ok "import aiscientist -> $imp" ;; *) die "aiscientist imports from $imp" ;; esac

  say "5/9 .env: rewrite $OLD -> $NEW (in place: keeps the file's owner, mode and ACL)"
  tmp="$(mktemp)"; sed -e "s#$OLD\\b#$NEW#g" "$NEW/app/.env" > "$tmp"
  diff "$BK/app.env" "$tmp" | sed 's/^/      /' || true
  cat "$tmp" > "$NEW/app/.env"; rm -f "$tmp"
  ! grep -q -F "$OLD" "$NEW/app/.env" || die ".env still names $OLD"
  ok ".env updated"

  say "6/9 database rows and run JSON: $OLD -> $NEW"
  db_rewrite "$OLD" "$NEW" "$BK/db-paths.json" save
  json_rewrite "$OLD" "$NEW" "$NEW/app/runs"

  say "7/9 systemd: $OLD_UNIT -> $NEW_UNIT"
  unit_text > "/etc/systemd/system/$NEW_UNIT"
  systemctl disable "$OLD_UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$OLD_UNIT"
  systemctl daemon-reload
  systemctl enable "$NEW_UNIT"
  ok "$NEW_UNIT enabled; $OLD_UNIT removed (copy in $BK)"

  say "8/9 sudoers: every /etc/sudoers.d grant that names $OLD_UNIT now names $NEW_UNIT"
  : > "$BK/sudoers-map.txt"
  for src in "$BK"/sudoers.d/*; do
    [ -f "$src" ] || continue
    base="$(basename "$src")"; dest="/etc/sudoers.d/$base"
    [ "$dest" = "$OLD_SUDOERS" ] && dest="$NEW_SUDOERS"
    # Built beside its destination under a name with a dot (sudo skips such files), checked, then
    # renamed into place: sudo never reads a half-written grant.
    tmp="/etc/sudoers.d/$base.new.$$"
    sed "s/${OLD_UNIT//./\\.}/$NEW_UNIT/g" "$src" > "$tmp"; chown root:root "$tmp"; chmod 0440 "$tmp"
    visudo -cf "$tmp" >/dev/null || { rm -f "$tmp"; die "the rewritten $base fails visudo -c (original left in place)"; }
    mv -f "$tmp" "$dest"
    [ "$dest" = "/etc/sudoers.d/$base" ] || rm -f "/etc/sudoers.d/$base"
    printf '%s\t%s\n' "$base" "$(basename "$dest")" >> "$BK/sudoers-map.txt"
    ok "$base -> $(basename "$dest")"
  done
  visudo -c >/dev/null || die "visudo -c fails after rewriting the grants — fix it now (originals: $BK/sudoers.d)"
  [ -s "$BK/sudoers-map.txt" ] || warn "no /etc/sudoers.d file named $OLD_UNIT — grant passwordless systemctl {start,stop,restart} $NEW_UNIT by hand"
  left="$(sudoers_mentions)"
  if [ -n "$left" ]; then
    warn "these sudoers lines still name ${OLD_UNIT%.service} and were NOT changed — review them by hand:"
    echo "$left" | sed 's/^/      /'
  fi

  say "9/9 start $NEW_UNIT and check it"
  systemctl start "$NEW_UNIT"; sleep 3
  systemctl is-active --quiet "$NEW_UNIT" || die "$NEW_UNIT is not active — journalctl -u $NEW_UNIT -n 80; then --rollback"
  pid="$(systemctl show -p MainPID --value "$NEW_UNIT")"
  ok "active, pid $pid, cwd $(readlink "/proc/$pid/cwd")"
  health || die "health check failed — journalctl -u $NEW_UNIT -n 80; then --rollback"

  say "done. Backups: $BK"
  cat <<EOF
    Check by hand now (docs: deploy/README.md "Moving the deployment root"):
      * log in; open an OLD run and its figures; continue one old run
      * Settings -> API keys: an existing key still lists its models (this resolves the secret)
      * connect with a saved SSH key (no password/Duo prompt means the key resolved)
      * pick an old uploaded dataset -> it binds (no "Not bound" warning)
    Logs:   journalctl -u $NEW_UNIT -f
    Undo:   sudo bash $0 --rollback
    Later:  remove the $OLD symlink once nothing names it (see the docs), then --harden
EOF
  exit 0
fi

# ---------------------------------------------------------------------------------------------
if [ "$MODE" = "rollback" ]; then
  [ -L "$OLD" ] && [ -d "$NEW" ] || die "nothing to roll back ($OLD is not the compatibility symlink)"
  BK="$(ls -d "$NEW"/env-backups/root-move-* 2>/dev/null | sort | tail -1)"
  [ -n "$BK" ] && [ -f "$BK/venv-files.tar" ] || die "no backup under $NEW/env-backups/root-move-*"
  say "rollback from $BK"
  systemctl stop "$NEW_UNIT" 2>/dev/null || true
  tar -C / -xpf "$BK/venv-files.tar"; ok "venv files restored"
  cat "$BK/app.env" > "$NEW/app/.env"; ok ".env restored"
  db_rewrite "$NEW" "$OLD" "$BK/db-paths.json"
  json_rewrite "$NEW" "$OLD" "$NEW/app/runs"
  systemctl disable "$NEW_UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$NEW_UNIT"
  cp -p "$BK/$OLD_UNIT" "/etc/systemd/system/$OLD_UNIT"
  if [ -s "$BK/sudoers-map.txt" ]; then
    while IFS=$'\t' read -r base dest; do
      install -m 0440 -o root -g root "$BK/sudoers.d/$base" "/etc/sudoers.d/$base"
      [ "$dest" = "$base" ] || rm -f "/etc/sudoers.d/$dest"
    done < "$BK/sudoers-map.txt"
    visudo -c >/dev/null && ok "sudoers grants restored"
  fi
  rm "$OLD"; mv "$NEW" "$OLD"
  install -d -m 0750 -o root -g "$SVC_USER" "$NEW"   # the empty directory that was there before
  systemctl daemon-reload; systemctl enable "$OLD_UNIT"; systemctl start "$OLD_UNIT"; sleep 3
  systemctl is-active --quiet "$OLD_UNIT" && health && say "rolled back: $OLD_UNIT serving from $OLD (backups kept in $OLD/${BK#"$NEW"/})"
  exit 0
fi

# ---------------------------------------------------------------------------------------------
if [ "$MODE" = "harden" ]; then
  # The tree inherited ACLs granting group `users` — on this host, nearly every human account —
  # rwx on the root and on env/: anyone could replace a package in the venv and run code as the
  # service account, which holds every user's HPC3 private key and API keys. This removes that
  # grant and other-access, matching how the empty $NEW was created (root:$SVC_USER 0750).
  # Admins keep access through `sudo -u $SVC_USER`. Coordinate with whoever reads the tree directly.
  [ -d "$NEW" ] && [ ! -L "$NEW" ] || die "$NEW not found — run --apply first"
  say "harden $NEW: drop the group:users ACL and other-access"
  # -n: leave each ACL mask as it is. Without it setfacl recalculates the mask when an entry goes,
  # and a directory that is closed to its group today (state/ and env-backups/ are 0700 through a
  # `---` mask) would come out group-accessible — a hardening step that widens access.
  setfacl -n -R -x g:users "$NEW" 2>/dev/null || true
  find "$NEW" -type d -exec setfacl -n -x d:g:users {} + 2>/dev/null || true
  chmod -R o-rwx "$NEW"
  find "$NEW" -type d -exec setfacl -n -m d:o::--- {} + 2>/dev/null || true
  ok "$(getfacl -p "$NEW" 2>/dev/null | grep -v '^#' | tr '\n' ' ')"
  # Permissions apply at once; no restart (it would drop live sessions). Just confirm it still serves.
  health || die "service unhealthy after hardening — check journalctl -u $NEW_UNIT"
  say "hardened."
fi
