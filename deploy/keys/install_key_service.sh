#!/usr/bin/env bash
# Install the AiScientist key service on the Eye Server (RCHENLAB/AiScientist issue 16).
#
# Run ONCE, as root (it creates a system account and a systemd unit):
#
#     sudo bash deploy/keys/install_key_service.sh
#
# What it does, in order. It changes nothing the web server uses, so the website keeps running as it is:
#   1. creates the system account aiscientist-keys (no shell, no home);
#   2. creates /etc/aiscientist-keys/token (a new random token; 0640 aiscientist-keys:aiscientist) and
#      /var/lib/aiscientist-keys (0700 aiscientist-keys);
#   3. COPIES the saved API keys (only the key files the index lists) and the master key from the web
#      server's state dir into /var/lib/aiscientist-keys. The originals stay where they are;
#   4. installs aiscientist-keys.service (and a drop-in that starts it before aiscientist.service),
#      plus a sudoers rule that lets the admin account restart it like aiscientist.service;
#   5. starts the service and checks that it holds and can decrypt as many keys as the web server lists.
#
# It does NOT switch the web server over and does NOT delete the originals. Then, as the admin
# account (no password needed):
#   - add to the web server's .env:
#         AISCIENTIST_KEY_SERVICE=http://127.0.0.1:8899
#         AISCIENTIST_KEY_SERVICE_TOKEN_FILE=/etc/aiscientist-keys/token
#   - when no run is running: sudo systemctl restart aiscientist.service
#   - check:  sudo -u aiscientist <python> -m aiscientist.gateway.admin_cli key-service-check
#   - then delete the web server's copies (as aiscientist): <state>/llm_creds/*/*.key and
#     <state>/llm_key.master. From then on only aiscientist-keys can read a key.
#
# Undo (before or after the switch), as root:  sudo bash deploy/keys/install_key_service.sh --copy-back
# puts the service's keys back into the web server's state dir. Remove the two .env lines and restart.
#
# Settings (environment): AISCIENTIST_ROOT (default /data/aiscientist), STATE (default $ROOT/state),
# PYTHON (default: $ROOT/app/current/.venv/bin/python if present, else $ROOT/env/bin/python),
# ADMIN_USER (default: $SUDO_USER), PORT (default 8899).
set -euo pipefail

ROOT=${AISCIENTIST_ROOT:-/data/aiscientist}
STATE=${STATE:-$ROOT/state}
KEYS=/var/lib/aiscientist-keys
TOKDIR=/etc/aiscientist-keys
PORT=${PORT:-8899}
ADMIN_USER=${ADMIN_USER:-${SUDO_USER:-}}
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "$ROOT/app/current/.venv/bin/python" ]]; then PYTHON=$ROOT/app/current/.venv/bin/python
  else PYTHON=$ROOT/env/bin/python; fi
fi

die() { echo "install_key_service: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || die "run as root (sudo bash $0)"
[[ -x "$PYTHON" ]] || die "no python at $PYTHON (set PYTHON=...)"

copy_keys() {  # copy_keys <from llm_creds> <to llm_creds>: the index and the key files it lists
  "$PYTHON" -I - "$1" "$2" <<'PY'
import json, os, shutil, sys
src, dst = sys.argv[1:3]
n = 0
for owner in sorted(os.listdir(src)):
    d = os.path.join(src, owner)
    idx = os.path.join(d, "index.json")
    if not os.path.isfile(idx):
        continue
    rows = json.load(open(idx, encoding="utf-8"))
    os.makedirs(os.path.join(dst, owner), mode=0o700, exist_ok=True)
    shutil.copy2(idx, os.path.join(dst, owner, "index.json"))
    for row in rows:
        f = os.path.join(d, f"{row['id']}.key")
        if os.path.isfile(f):
            shutil.copy2(f, os.path.join(dst, owner, f"{row['id']}.key"))
            n += 1
print(n)
PY
}

if [[ "${1:-}" == "--copy-back" ]]; then
  [[ -d "$KEYS/llm_creds" ]] || die "nothing to copy back: $KEYS/llm_creds is missing"
  install -d -m 0700 -o aiscientist -g aiscientist "$STATE/llm_creds"
  n=$(copy_keys "$KEYS/llm_creds" "$STATE/llm_creds")
  [[ -f "$KEYS/llm_key.master" ]] && install -m 0600 -o aiscientist -g aiscientist "$KEYS/llm_key.master" "$STATE/llm_key.master"
  chown -R aiscientist:aiscientist "$STATE/llm_creds"
  echo "Copied $n key file(s) and the master key back into $STATE."
  echo "Now remove AISCIENTIST_KEY_SERVICE* from the web server's .env and restart aiscientist.service."
  exit 0
fi

[[ -d "$STATE/llm_creds" ]] || die "no key store at $STATE/llm_creds"
if ss -ltn "sport = :$PORT" | grep -q LISTEN && ! systemctl is-active --quiet aiscientist-keys.service; then
  die "port $PORT is taken by something else (set PORT=...)"
fi

echo "== 1. account"
id aiscientist-keys >/dev/null 2>&1 || \
  useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin aiscientist-keys
sudo -u aiscientist-keys "$PYTHON" -c "import aiscientist.gateway.key_service" \
  || die "aiscientist-keys cannot import the code with $PYTHON"

echo "== 2. token and store"
install -d -m 0750 -o aiscientist-keys -g aiscientist "$TOKDIR"
if [[ ! -s "$TOKDIR/token" ]]; then
  (umask 077; head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$TOKDIR/token")
fi
chown aiscientist-keys:aiscientist "$TOKDIR/token"; chmod 0640 "$TOKDIR/token"
install -d -m 0700 -o aiscientist-keys -g aiscientist-keys "$KEYS"

echo "== 3. copy the keys (the originals stay)"
if [[ -d "$KEYS/llm_creds" ]]; then
  echo "   $KEYS/llm_creds already exists: not copied again"
else
  install -d -m 0700 "$KEYS/llm_creds"
  n=$(copy_keys "$STATE/llm_creds" "$KEYS/llm_creds")
  [[ -f "$STATE/llm_key.master" ]] && install -m 0600 "$STATE/llm_key.master" "$KEYS/llm_key.master"
  echo "   copied $n key file(s) and the master key"
fi
chown -R aiscientist-keys:aiscientist-keys "$KEYS"
chmod -R u=rwX,go= "$KEYS"
command -v setfacl >/dev/null && setfacl -R -b "$KEYS"
sudo -u aiscientist test -r "$KEYS/llm_key.master" 2>/dev/null && die "aiscientist can still read $KEYS: stop"

echo "== 4. units and sudoers"
sed "s#@PYTHON@#$PYTHON#; s#--port 8899#--port $PORT#" "$HERE/../systemd/aiscientist-keys.service" \
  > /etc/systemd/system/aiscientist-keys.service
install -d /etc/systemd/system/aiscientist.service.d
install -m 0644 "$HERE/../systemd/aiscientist.service.d/key-service.conf" \
  /etc/systemd/system/aiscientist.service.d/key-service.conf
if [[ -n "$ADMIN_USER" ]]; then
  rule="$ADMIN_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart aiscientist-keys.service, /usr/bin/systemctl start aiscientist-keys.service, /usr/bin/systemctl stop aiscientist-keys.service"
  tmp=$(mktemp); echo "$rule" > "$tmp"
  visudo -cf "$tmp" >/dev/null && install -m 0440 "$tmp" /etc/sudoers.d/aiscientist-keys
  rm -f "$tmp"
fi
systemctl daemon-reload
systemctl enable --now aiscientist-keys.service
systemctl restart aiscientist-keys.service

echo "== 5. check"
for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 0.5; done
status=$(curl -fsS -H "Authorization: Bearer $(cat "$TOKDIR/token")" "http://127.0.0.1:$PORT/v1/status") \
  || die "the key service does not answer (journalctl -u aiscientist-keys)"
listed=$(sudo -u aiscientist env -u AISCIENTIST_KEY_SERVICE AISCIENTIST_STATE_DIR="$STATE" "$PYTHON" -c \
  "from aiscientist.gateway import llm_credentials as c; print(sum(1 for o, r in c.all_credentials() if not r.get('session_only')))")
echo "   key service: $status"
echo "   web server lists $listed saved key(s)"
"$PYTHON" -I -c "import json,sys; s=json.loads(sys.argv[1]); n=int(sys.argv[2]); saved=s['held']-s['session_keys']; sys.exit(0 if saved==n and s['readable']>=n else 1)" \
  "$status" "$listed" || die "the counts differ: the web server was NOT switched; nothing was removed"
echo
echo "Done. The key service holds and can read all $listed saved key(s). The web server still uses its own"
echo "copies until AISCIENTIST_KEY_SERVICE is set in its .env and it restarts (see the top of this script)."
